import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from kubernetes import client, config

from . import k8s
from .auth import get_username
from .config import settings
from .idle import idle_reaper_loop
from .models import PoolStatus, ScaleRequest


def _load_kube_config() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


_load_kube_config()
apps_api = client.AppsV1Api()
core_api = client.CoreV1Api()


@asynccontextmanager
async def lifespan(app: FastAPI):
    reaper_task = asyncio.create_task(idle_reaper_loop(apps_api)) if settings.idle_reaper_enabled else None
    try:
        yield
    finally:
        if reaper_task is not None:
            reaper_task.cancel()


app = FastAPI(title="taskvine-gateway", lifespan=lifespan)


def _status_from(statefulset) -> PoolStatus:
    username = statefulset.metadata.labels[k8s.USER_LABEL]
    return PoolStatus(
        username=username,
        desired_replicas=statefulset.spec.replicas,
        ready_replicas=statefulset.status.ready_replicas or 0,
        manager_host=f"{k8s.manager_service_name(username)}.{settings.namespace}.svc.cluster.local",
        manager_port=settings.manager_port,
    )


@app.put("/pools/me", response_model=PoolStatus)
def scale_my_pool(req: ScaleRequest, username: str = Depends(get_username)) -> PoolStatus:
    """Create-if-absent and set the caller's desired worker replica count.

    `username` comes only from the validated JupyterHub token (see auth.py) -
    a caller can never name a different user's pool.
    """
    sts = k8s.ensure_worker_pool(apps_api, core_api, username, req.replicas)
    return _status_from(sts)


@app.get("/pools/me", response_model=PoolStatus)
def get_my_pool(username: str = Depends(get_username)) -> PoolStatus:
    sts = k8s.get_worker_pool(apps_api, username)
    if sts is None:
        raise HTTPException(status_code=404, detail="No worker pool for this user yet")
    return _status_from(sts)


@app.delete("/pools/me", status_code=204)
def delete_my_pool(username: str = Depends(get_username)) -> None:
    k8s.delete_worker_pool(apps_api, core_api, username)


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
