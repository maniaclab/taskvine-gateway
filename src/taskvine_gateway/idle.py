import asyncio
import json
import logging
import time

from kubernetes import client

from . import k8s
from .config import settings

logger = logging.getLogger(__name__)

# Tracks, per username, the last time their manager was seen with any
# waiting/running task, and separately how long their pool has sat at 0
# replicas. In-memory only - fine because the gateway runs as a single
# replica; a restart just resets both clocks for everyone, which is a
# harmless (if slightly conservative) worst case.
_last_active: dict[str, float] = {}
_zero_since: dict[str, float] = {}


async def query_manager_status(host: str, port: int, timeout: float = 5.0) -> dict | None:
    """Speaks the same plain-text protocol `vine_status` uses for a direct
    query: connect, send "manager_status\\n", read back a JSON array with
    one object (tasks_waiting, tasks_running, tasks_complete, workers).
    Returns None if the manager can't be reached (e.g. notebook not
    running) - that's treated as "no signal", not "definitely idle", by the
    caller.
    """
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None

    try:
        writer.write(b"manager_status\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(65536), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        writer.close()

    try:
        parsed = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
        return parsed[0]
    return None


def _is_idle(status: dict) -> bool:
    return status.get("tasks_waiting", 0) == 0 and status.get("tasks_running", 0) == 0


async def _reap_zero_replica_pool(apps_api: client.AppsV1Api, core_api: client.CoreV1Api, username: str, now: float) -> None:
    """A pool already at 0 replicas - either the idle check just scaled it
    down, a user scaled it to 0 themselves, or it was already there from a
    prior tick. Deleting immediately would lose the "resume is a cheap
    patch, not a fresh create" benefit of scale-to-zero, so give it a
    longer grace window before tearing down the StatefulSet/Service
    entirely.
    """
    zero_since = _zero_since.setdefault(username, now)
    zero_for = now - zero_since
    if zero_for < settings.delete_after_zero_seconds:
        return

    logger.info("deleting %s's worker pool after sitting at 0 replicas for %.0fs", username, zero_for)
    await asyncio.to_thread(k8s.delete_worker_pool, apps_api, core_api, username)
    _zero_since.pop(username, None)
    _last_active.pop(username, None)


async def _reap_once(apps_api: client.AppsV1Api, core_api: client.CoreV1Api) -> None:
    # apps_api's calls are synchronous (blocking) - run them off the event
    # loop so a slow k8s API call doesn't stall concurrent HTTP requests.
    pools = await asyncio.to_thread(
        apps_api.list_namespaced_stateful_set, settings.namespace, label_selector=f"app={k8s.WORKER_APP_LABEL}"
    )
    now = time.monotonic()

    for pool in pools.items:
        username = pool.metadata.labels.get(k8s.USER_LABEL)
        if not username:
            continue

        if not pool.spec.replicas:
            await _reap_zero_replica_pool(apps_api, core_api, username, now)
            continue

        # Pool has replicas again (user scaled back up, or just created) -
        # the zero-replica clock no longer applies.
        _zero_since.pop(username, None)

        host = f"{k8s.manager_service_name(username)}.{settings.namespace}.svc.cluster.local"
        status = await query_manager_status(host, settings.manager_port)

        if status is not None and not _is_idle(status):
            _last_active[username] = now
            continue

        # status is None (manager unreachable) or idle - either way, no
        # work is happening for this pool right now.
        last_active = _last_active.setdefault(username, now)
        idle_for = now - last_active
        if idle_for < settings.idle_timeout_seconds:
            continue

        logger.info("scaling %s's idle worker pool to 0 after %.0fs idle", username, idle_for)
        pool.spec.replicas = 0
        await asyncio.to_thread(apps_api.patch_namespaced_stateful_set, pool.metadata.name, settings.namespace, pool)
        _last_active.pop(username, None)
        _zero_since[username] = now


async def idle_reaper_loop(apps_api: client.AppsV1Api, core_api: client.CoreV1Api) -> None:
    while True:
        try:
            await _reap_once(apps_api, core_api)
        except Exception:
            logger.exception("idle reaper tick failed")
        await asyncio.sleep(settings.idle_check_interval_seconds)
