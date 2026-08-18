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

## Configuration

Env vars (prefix `TVG_`), see `src/taskvine_gateway/config.py`. Per-cluster
values that must be overridden (they vary across rp1-dev/rp1/odf, mirroring
`patch-storage.yaml`'s differing storage classes):

- `TVG_WORKER_WORKSPACE_STORAGE_CLASS` - e.g. `odf-ceph-rbd`, `iu-ceph-block`
- `TVG_NAMESPACE` - defaults to `jupyterhub`

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
service's own pod.

## Running locally

```bash
pip install -e .
export JUPYTERHUB_API_TOKEN=... JUPYTERHUB_API_URL=...
export TVG_WORKER_WORKSPACE_STORAGE_CLASS=standard
taskvine-gateway
```
