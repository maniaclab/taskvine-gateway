from pydantic import BaseModel, Field


class ScaleRequest(BaseModel):
    replicas: int = Field(ge=0, description="Desired number of vine_worker replicas")

    # Per-pool overrides. Omit either to use the deployment's default (see
    # config.py's worker_cores/worker_memory_mb). Each is bounded server-side
    # - per-worker (config.py's max_worker_*) and pool-wide, i.e. replicas *
    # cores or replicas * memory_mb (config.py's max_pool_*) - and rejected
    # outright, not silently clamped, if it exceeds either ceiling.
    #
    # There's no disk_mb override: the worker's advertised disk is derived
    # from workspace_size_gb (disk_mb = workspace_size_gb * 1024) rather
    # than being independently settable, so it can never exceed what the
    # real volume backing /workspace actually holds.
    cores: int | None = Field(default=None, ge=1, description="Override worker cores")
    memory_mb: int | None = Field(default=None, ge=1, description="Override worker memory in MB")

    # There's no workspace_kind override: whether workers get an emptyDir
    # or a PVC is a deployment decision (config.py's worker_workspace_kind),
    # not a per-user one - letting a caller pick "pvc" would let them
    # provision arbitrary PVCs against the deployment's storage class,
    # which isn't something a caller should be able to do on their own.
    #
    # workspace_size_gb only takes effect when the pool doesn't already
    # exist: a StatefulSet's volumeClaimTemplates are immutable in
    # Kubernetes once created, so a request to change it for an existing
    # pool is rejected too - delete the pool first (DELETE /pools/me) if
    # you need to change it.
    workspace_size_gb: int | None = Field(
        default=None, ge=1, description="Override workspace size in GB - also determines the worker's advertised disk"
    )


class PoolStatus(BaseModel):
    username: str
    desired_replicas: int
    ready_replicas: int
    manager_host: str
    manager_port: int
    cores: int
    memory_mb: int
    disk_mb: int
    workspace_kind: str
    workspace_size_gb: int
