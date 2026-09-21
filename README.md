# taskvine-gateway

Lets a JupyterHub notebook user spin up/down their own pool of
[TaskVine](https://ccl.cse.nd.edu/software/taskvine/) `vine_worker` pods on
Kubernetes, authenticated as themselves via their existing JupyterHub API
token - the same trust model [dask-gateway](https://gateway.dask.org/) uses
for Dask (`gateway.auth.type: jupyterhub`).

## Requirements

Unlike `dask-gateway`, which owns its whole worker/scheduler lifecycle
independently, a TaskVine worker has to dial back into the *manager
running inside the requesting user's own notebook pod* - that's what
makes this fundamentally different to deploy, and worth reading before
setting it up on a new cluster:

- **[zero-to-jupyterhub](https://z2jh.jupyter.org/) with `KubeSpawner`.**
  Not just "any JupyterHub spawner" - the gateway has to find a specific
  user's already-running notebook pod, and does so the same way
  KubeSpawner itself would (its `hub.jupyter.org/username` pod label -
  see "Username handling" below).
- **One notebook server per user** (the z2jh/KubeSpawner default).
  Named servers (`--allow-named-servers`) aren't supported: the
  manager Service's selector matches on username alone, so with
  multiple servers per user it could route to whichever one KubeSpawner
  happens to select, not necessarily the one actually running the
  TaskVine manager.
- **A worker-reachable path to the manager port.** If the deployment's
  z2jh chart has `singleuser.networkPolicy.enabled: true` (the z2jh
  default), worker pods need an explicit allow-rule to reach it - see
  "Required JupyterHub-side wiring" below.
- **Your own worker image.** `worker_image` has no usable default -
  build one from `worker/` in this repo (see "Worker image" below).

## API

Every request must carry `Authorization: token <JUPYTERHUB_API_TOKEN>` -
the token already present in every singleuser pod's environment. The
username is always derived from that token (via `HubAuth`), never from
client input, so a caller can only ever affect their own pool.

- `PUT /pools/me` `{"replicas": N, ...}` - create-if-absent, set desired worker count and (optionally) per-pool resource/workspace overrides - see "Per-pool overrides" below
- `GET /pools/me` - status (desired vs ready replicas, manager address, resolved cores/memory/disk/workspace config)
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
cluster.status()           # {'username': ..., 'desired_replicas': 10, 'ready_replicas': 3, 'cores': 1, ...}

# Override this pool's per-worker resources (see "Per-pool overrides" below) -
# omit any of these to use the gateway's default.
cluster.scale(10, cores=2, memory_mb=4000)

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

`TVG_WORKER_CORES` / `TVG_WORKER_MEMORY_MB` are the deployment's *default*
worker footprint - they set both the `vine_worker --cores/--memory` flags
and the pod's k8s `resources.requests/limits` (request == limit, so a
worker can't balloon past what it advertises to the manager).

Worker scratch space (`/workspace`, `vine_worker`'s working directory for
per-task files) defaults to an `emptyDir`, sized by `TVG_WORKER_WORKSPACE_SIZE_GB`
- deliberately not persistent, the same way dask-gateway's own workers use
local ephemeral storage rather than a volume. Set
`TVG_WORKER_WORKSPACE_KIND=pvc` (plus `TVG_WORKER_WORKSPACE_STORAGE_CLASS`)
if a deployment needs scratch to survive a pod restart or needs more space
than local node disk offers.

### Per-pool overrides

A caller can override their own pool's `cores`/`memory_mb` and
`workspace_size_gb` per `PUT /pools/me` call (or via
`TaskVineCluster.scale()`'s matching keyword arguments) instead of using
the deployment's defaults above - the same idea as `dask-gateway`'s own
`new_cluster(worker_cores=..., worker_memory=...)`. There's no `disk_mb`
override - see "Worker resources and scratch space" above for why it's
derived from `workspace_size_gb` instead.

`cores`/`memory_mb` are checked against **two** independent ceilings, and
a request exceeding either is rejected outright (`422`), not silently
clamped down to it:

- **Per-worker** (`TVG_MAX_WORKER_CORES` / `TVG_MAX_WORKER_MEMORY_MB` /
  `TVG_MAX_WORKER_WORKSPACE_SIZE_GB`) - bounds a single worker's footprint,
  regardless of how many replicas the pool has.
- **Pool-wide** (`TVG_MAX_POOL_CORES` / `TVG_MAX_POOL_MEMORY_MB`) - bounds
  `replicas * cores` and `replicas * memory_mb` across the whole pool,
  mirroring `dask-gateway`'s own `cluster_max_cores`/`cluster_max_memory`
  (a cap on the *whole cluster's* total footprint, not any one worker's).

`cores`/`memory_mb` can be changed on an existing pool at any time - the
change rolls out to workers via the underlying `StatefulSet`'s normal
update mechanics. `workspace_kind`/`workspace_size_gb` can only be set the
first time a pool is created: Kubernetes doesn't allow changing a
`StatefulSet`'s volume claim templates in place, so a request to change
either on a pool that already exists is rejected (`409`) - `DELETE
/pools/me` first if you need to change them.

### PVC mounts

`TVG_WORKER_PVC_MOUNTS` is a JSON array of PVCs to mount into every
worker pod - empty by default (no PVCs mounted unless configured). Each
entry's `claim_name_template` and `mount_path_template` support
`{username}` substitution for a per-user claim, or can be used as-is (no
`{username}`) for a single PVC shared across all users - e.g. a per-user
data volume alongside a shared, read-only reference dataset. `{username}`
here is a slug, not the caller's raw username - see "Username handling"
below, since a per-user claim name only actually matches an existing PVC
if it resolves the same way KubeSpawner's own `{username}` templating
(or `pre_spawn_hook`) already resolved it for that PVC:

```bash
export TVG_WORKER_PVC_MOUNTS='[
  {"name": "user-data", "claim_name_template": "user-data-{username}", "mount_path_template": "/data/{username}"},
  {"name": "reference-dataset", "claim_name_template": "reference-dataset", "mount_path_template": "/mnt/reference", "read_only": true}
]'
```

Each entry's `name` must be unique and not collide with `workspace` (the
other built-in volume name).

## Worker image

`TVG_WORKER_IMAGE` has no usable default - there's no single image every
deployment could pull, so it must be set explicitly. Build one from
`worker/` in this repo (or extend it, e.g. to add commonly-needed task
dependencies to the base image rather than shipping them per-task with
poncho - see below) and publish it somewhere your cluster can pull from.

This repo's own CI (`.github/workflows/build-worker-image.yml`) builds
and publishes `worker/` to `ghcr.io/maniaclab/taskvine-gateway-worker`
on every push to `main`, as this project's own reference build - not a
generic default any deployment can rely on (its visibility/access is
whatever this repo's own org has set, and it's only rebuilt when this
repo's own `worker/` changes).

`ndcctools` is baked into the image at build time rather than resolved
from conda-forge on every pod start, so a worker pod is just scheduling +
a (node-cached) image pull + `exec vine_worker`, with no dependency on
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

If z2jh's own `singleuser.networkPolicy.enabled` is `true` (the z2jh
default), a worker pod's connection to the manager port
(`TVG_MANAGER_PORT`, default `9123`) on the notebook pod is blocked
unless something explicitly allows it - workers just never connect,
with no error visible anywhere in this service. Add an ingress rule
matching every worker pod's `app` label (`TVG_WORKER_APP_LABEL`,
default `taskvine-worker`):

```yaml
singleuser:
  networkPolicy:
    ingress:
      - from:
          - podSelector:
              matchLabels:
                app: taskvine-worker
        ports:
          - port: 9123
```

## Username handling

A caller is always identified by their raw Hub username (from the
validated token, via `HubAuth`) - but that raw username is never used
directly as a Kubernetes object name or label value, since it can
contain characters neither allows (e.g. `jzhou24@nd.edu`). Instead it's
escaped the same way KubeSpawner itself escapes it for its own
`{username}`-templated resource names and its singleuser pod's
`hub.jupyter.org/username` label (see `src/taskvine_gateway/slugs.py`,
a thin wrapper around [`escapism`](https://github.com/jupyterhub/escapism) -
the same package KubeSpawner uses).

This has to match whatever that deployment's z2jh chart is actually
configured with, via `TVG_USERNAME_SLUG_SCHEME` (`safe`, the default and
KubeSpawner's own current default, or `escape`, KubeSpawner's older but
still-supported scheme - check `hub.kubespawner.slug_scheme` in that
chart's own values). Get this wrong and every name/label/mount path this
service computes silently stops matching what KubeSpawner actually
created - the manager Service's selector matches zero pods, a per-user
PVC mount points at a PVC that doesn't exist, etc.

## Required RBAC

The `charts/taskvine-gateway` Helm chart below templates this for you
(see its `templates/role.yaml`) unless `serviceAccount.create: false`. If
deploying without it, the gateway needs to create/read/update/delete
`StatefulSet`s and `Service`s in its own namespace:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: taskvine-gateway
rules:
  - apiGroups: ["apps"]
    resources: ["statefulsets"]
    verbs: ["get", "list", "watch", "create", "patch", "delete"]
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["get", "list", "watch", "create", "delete"]
```

bound to the `ServiceAccount` its `Deployment` runs as, via a matching
`RoleBinding` in the same namespace.

## Server image

Same idea as the worker image above, but for the gateway server itself:
`Dockerfile` (repo root) bakes the server and its dependencies in at
build time rather than installing them from a pinned git rev on every
pod start. `TVG_*`-driven config, not build-time config, so one image
serves every deployment. This repo's own CI
(`.github/workflows/build-server-image.yml`) publishes it to
`ghcr.io/maniaclab/taskvine-gateway` as this project's own reference
build, same caveat as the worker image: not a generic public default,
build your own if you're not this project.

## Deploying

`charts/taskvine-gateway` is a Helm chart covering everything above -
the `Deployment`/`Service`/`ServiceAccount`/`Role`/`RoleBinding`, and
optionally a `NetworkPolicy` (`networkPolicy.enabled`) restricting
ingress to the gateway's own API to singleuser pods. See its
`values.yaml` for the full set of values and what each maps to; at
minimum a deployment needs to set `worker.image` and
`jupyterhub.apiUrl`/`apiToken` - none of which have a usable generic
default, for the reasons explained above. `image.repository` (the
gateway's own image) does have one, since unlike the worker image
there's no reason a deployment would need a different one - override it
only for a private mirror or your own build of this repo.

```bash
helm install taskvine-gateway ./charts/taskvine-gateway \
  --namespace jupyterhub \
  --set worker.image=ghcr.io/maniaclab/taskvine-gateway-worker \
  --set jupyterhub.apiUrl=http://hub.jupyterhub.svc.cluster.local:8081/hub/api \
  --set jupyterhub.apiToken=... # or use -f values.yaml / --set-file
```

Flux-managed deployments (this project's own included) more typically
source the chart straight from this repo via a `GitRepository`, pinned
to a commit, rather than `helm install` directly - see rp1-core's own
`infrastructure/taskvine-gateway/` for a worked example.

## Running the server locally

```bash
pip install -e ".[server]"
export JUPYTERHUB_API_TOKEN=... JUPYTERHUB_API_URL=...
taskvine-gateway
```
