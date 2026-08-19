"""Lightweight client for taskvine-gateway.

Stdlib-only on purpose - this is what gets imported from inside a
notebook, and shouldn't drag in the gateway's own server-side
dependencies (fastapi, the kubernetes client, jupyterhub) just to make a
few HTTP calls. See the `server` extra in pyproject.toml for those.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class TaskVineGatewayError(RuntimeError):
    """Raised when the gateway rejects a request or can't be reached."""


class TaskVineCluster:
    """A handle to the caller's own TaskVine worker pool.

    >>> cluster = TaskVineCluster()
    >>> cluster.scale(10)
    >>> cluster.status()
    {'username': 'alice', 'desired_replicas': 10, 'ready_replicas': 3, ...}
    >>> cluster.close()

    `address` and `token` default to the TASKVINE_GATEWAY_ADDRESS and
    JUPYTERHUB_API_TOKEN environment variables - JupyterHub already injects
    both into every singleuser pod, so no configuration is needed from
    inside a notebook. Pass them explicitly to use this outside one.
    """

    def __init__(self, address: str | None = None, token: str | None = None, timeout: float = 10.0):
        address = address or os.environ.get("TASKVINE_GATEWAY_ADDRESS")
        token = token or os.environ.get("JUPYTERHUB_API_TOKEN")
        if not address:
            raise TaskVineGatewayError(
                "No gateway address given and TASKVINE_GATEWAY_ADDRESS is not set - "
                "pass address= explicitly if you're not running inside a notebook server."
            )
        if not token:
            raise TaskVineGatewayError(
                "No token given and JUPYTERHUB_API_TOKEN is not set - "
                "pass token= explicitly if you're not running inside a notebook server."
            )

        self._url = address.rstrip("/") + "/pools/me"
        self._headers = {"Authorization": f"token {token}", "Content-Type": "application/json"}
        self._timeout = timeout

    def _request(self, method: str, body: dict | None = None) -> dict | None:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self._url, data=data, headers=self._headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise TaskVineGatewayError(f"{method} {self._url} -> {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise TaskVineGatewayError(f"couldn't reach taskvine-gateway at {self._url}: {e.reason}") from e

    def scale(self, n: int) -> dict:
        """Set the desired worker count, creating the pool on first call."""
        return self._request("PUT", {"replicas": n})

    def status(self) -> dict:
        """Current pool status: desired vs ready replicas, manager address."""
        return self._request("GET")

    def close(self) -> None:
        """Tear down the pool immediately.

        Not required to call - an idle pool is scaled to 0 and eventually
        deleted on its own (see the gateway's idle reaper) - but this is
        immediate if you're done for sure.
        """
        self._request("DELETE")

    def __enter__(self) -> "TaskVineCluster":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
