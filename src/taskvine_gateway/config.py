from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TVG_")

    # Namespace singleuser pods and workers live in on this cluster.
    namespace: str = "jupyterhub"

    # Worker pod image + how it resolves ndcctools (mirrors the hand-built
    # prototype in clusters/*/infrastructure/taskvine-workers).
    worker_image: str = "ghcr.io/prefix-dev/pixi:0.76.1"
    worker_pixi_configmap: str = "taskvine-worker-pixi"

    # "emptydir" (default) matches how dask-gateway's own workers handle
    # scratch here - no PVC at all, just local ephemeral storage, since
    # worker scratch is disposable and doesn't need Ceph-backed persistence.
    # It also avoids a per-replica Ceph RBD PVC create/delete on every scale
    # event. "pvc" is available for clusters/workloads that need scratch to
    # survive a pod restart or need more space than local node disk offers.
    worker_workspace_kind: Literal["emptydir", "pvc"] = "emptydir"
    worker_workspace_size_limit: str = "5Gi"

    # Only used when worker_workspace_kind == "pvc". Storage classes differ
    # across clusters (e.g. odf-ceph-rbd vs iu-ceph-block) so must be set via
    # env in the cluster overlay, not left at this default.
    worker_workspace_storage_class: str = "REPLACE_ME"
    worker_workspace_storage_size: str = "5Gi"

    manager_port: int = 9123

    # Single source of truth for a worker's resources, in the units
    # vine_worker's own --cores/--memory/--disk flags expect (whole cores,
    # MB). The k8s container's resources.requests/limits are derived from
    # these same values (see k8s.py) so what's advertised to the manager
    # always matches what the pod is actually allocated - request == limit
    # (Guaranteed QoS) for the same reason dask-gateway's workers do here:
    # so a worker pod can't balloon past its declared footprint.
    worker_cores: int = 1
    worker_memory_mb: int = 2000
    worker_disk_mb: int = 4000

    # Naming templates - {username} is substituted by str.format.
    manager_service_name_template: str = "taskvine-manager-{username}"
    worker_statefulset_name_template: str = "taskvine-worker-{username}"
    shared_data_pvc_template: str = "shared-data-{username}"

    max_workers_per_user: int = 20

    # Idle-pool reaper: periodically asks each pool's manager (the same
    # "manager_status" query `vine_status` uses - a plain TCP line, see
    # idle.py) whether it has any waiting/running tasks. If a pool has had
    # none for idle_timeout_seconds straight, it's scaled to 0 replicas.
    idle_reaper_enabled: bool = True
    idle_check_interval_seconds: int = 60
    idle_timeout_seconds: int = 3600

    # Once a pool has been scaled to 0 replicas, it's kept around at 0 (a
    # cheap resume via patch, not a fresh create) until it's sat idle at
    # zero for this long, after which the StatefulSet/Service are deleted
    # entirely. Default: 24h grace window before full teardown.
    delete_after_zero_seconds: int = 86400


settings = Settings()
