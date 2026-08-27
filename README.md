# taskvine-gateway

Lets a JupyterHub notebook user spin up/down their own pool of
[TaskVine](https://ccl.cse.nd.edu/software/taskvine/) `vine_worker` pods on
Kubernetes, authenticated as themselves via their existing JupyterHub API
token - the same trust model [dask-gateway](https://gateway.dask.org/) uses
for Dask (`gateway.auth.type: jupyterhub`).

## API

Every request must carry `Authorization: token <JUPYTERHUB_API_TOKEN>` -
the token already present in every singleuser pod's environment. The
username is always derived from that token (via `HubAuth`), never from
client input, so a caller can only ever affect their own pool.

- `PUT /pools/me` `{"replicas": N}` - create-if-absent, set desired worker count
- `GET /pools/me` - status (desired vs ready replicas, manager address)
- `DELETE /pools/me` - tear down

### Scaling from a notebook

```bash
pip install taskvine-gateway
```

installs just the client (`taskvine_gateway.TaskVineCluster`) - it's
stdlib-only, so this doesn't pull in the gateway's own server-side
dependencies. It reads `TASKVINE_GATEWAY_ADDRESS` and
`JUPYTERHUB_API_TOKEN` from the environment by default, both of which the
Hub already injects into every singleuser pod, so no configuration is
needed from inside a notebook:

```python
from taskvine_gateway import TaskVineCluster

cluster = TaskVineCluster()
cluster.scale(10)          # creates the pool on first call
cluster.status()           # {'username': ..., 'desired_replicas': 10, 'ready_replicas': 3, ...}

# Not required - an idle pool is scaled to 0 and eventually deleted on its
# own (see "Idle pools" below) - but this is immediate if you're done for
# sure. Also works as a context manager: `with TaskVineCluster() as cluster:`.
cluster.close()
```

## Idle pools

A background loop periodically asks each pool's manager - the same
`manager_status` query [`vine_status`](https://cctools.readthedocs.io/en/stable/man_pages/vine_status)
uses - whether it has any waiting or running tasks. A pool with neither for
`TVG_IDLE_TIMEOUT_SECONDS` (default 1h, matching dask-gateway's own
`idle_timeout` here) is scaled to 0 replicas; an unreachable manager (e.g.
the notebook itself has shut down) counts the same as idle. A pool sitting
at 0 replicas for `TVG_DELETE_AFTER_ZERO_SECONDS` (default 24h) has its
`StatefulSet`/`Service` deleted entirely, rather than kept around
indefinitely - scaling back up before then is a cheap patch to the
existing objects; after, it's a fresh create.

## Worker resources and scratch space

`TVG_WORKER_CORES` / `TVG_WORKER_MEMORY_MB` / `TVG_WORKER_DISK_MB` are the
single source of truth for a worker's footprint - they set both the
`vine_worker --cores/--memory/--disk` flags and the pod's k8s
`resources.requests/limits` (request == limit, so a worker can't balloon
past what it advertises to the manager).

Worker scratch space (`/workspace`, `vine_worker`'s working directory for
per-task files) defaults to an `emptyDir` - deliberately not persistent,
the same way dask-gateway's own workers use local ephemeral storage
rather than a volume. Set `TVG_WORKER_WORKSPACE_KIND=pvc` (plus
`TVG_WORKER_WORKSPACE_STORAGE_CLASS` and
`TVG_WORKER_WORKSPACE_STORAGE_SIZE`) if a deployment needs scratch to
survive a pod restart or needs more space than local node disk offers.

## Worker image

`worker/` builds the image workers actually run
(`ghcr.io/maniaclab/taskvine-gateway-worker`, published by
`.github/workflows/build-worker-image.yml` on push to `main`) -
`ndcctools` is baked in at build time rather than resolved from
conda-forge on every pod start, so a worker pod is just scheduling + a
(node-cached) image pull + `exec vine_worker`, with no dependency on
conda-forge being reachable at pod start and no initContainer at all.
Each submitted *task*'s own execution environment is a separate, later
concern, unrelated to what this image provides: package it with
[`poncho`](https://cctools.readthedocs.io/en/latest/poncho/) from the
notebook kernel (`poncho_package_create`), declare it to the manager
(`m.declare_poncho("my_env.tar.gz")`), and pass it per-computation
(`environment=` for `DaskVine`, or per-`Task` for plain TaskVine).
TaskVine ships and unpacks it on whichever worker runs that task,
regardless of what's in the worker's own base image.

## Configuration

Env vars (prefix `TVG_`); see `src/taskvine_gateway/config.py` for the full
list and defaults. Values that are genuinely deployment-specific and
should be set by whoever deploys this (storage classes, resource limits,
timeouts, etc. all vary by cluster and workload) rather than left at their
defaults.

## Required JupyterHub-side wiring

This service must be registered as a JupyterHub service (own API token) the
same way dask-gateway is, so it can validate other users' tokens via the
Hub API:

```yaml
hub:
  services:
    taskvine-gateway:
      url: "http://taskvine-gateway.jupyterhub.svc.cluster.local:8080/"
```

with a `JUPYTERHUB_API_TOKEN`/`JUPYTERHUB_API_URL` pair injected into this
service's own pod, and `TASKVINE_GATEWAY_ADDRESS` injected into singleuser
pods for the snippet above.

## Running the server locally

```bash
pip install -e ".[server]"
export JUPYTERHUB_API_TOKEN=... JUPYTERHUB_API_URL=...
taskvine-gateway
```
