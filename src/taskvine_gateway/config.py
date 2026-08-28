from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerMount(BaseModel):
    """One PVC to mount into every worker pod. claim_name_template and
    mount_path_template both support {username} substitution (str.format)
    for per-user claims; a template with no {username} placeholder is used
    as-is, so a single PVC shared across all users (e.g. a read-only
    reference dataset) works the same way with no special-casing.
    """

    name: str
    claim_name_template: str
    mount_path_template: str
    read_only: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TVG_")

    # Namespace singleuser pods and workers live in on this cluster.
    namespace: str = "jupyterhub"

    # Purpose-built image (worker/Dockerfile in this repo) with ndcctools
    # baked in at build time - no runtime install step, so no dependency on
    # conda-forge being reachable at pod start, and no initContainer at all.
    worker_image: str = "ghcr.io/maniaclab/taskvine-gateway-worker:latest"

    # Name of a kubernetes.io/dockerconfigjson Secret (in `namespace`) to
    # pull worker_image with, if it's not publicly readable. Empty (default)
    # means don't set imagePullSecrets at all - only needed when the image's
    # registry requires auth.
    worker_image_pull_secret: str = ""

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

    # PVCs to mount into every worker pod - a per-user data PVC, a shared
    # read-only reference dataset, etc. Set via TVG_WORKER_PVC_MOUNTS as a
    # JSON array of WorkerMount objects. Empty by default - no PVCs are
    # mounted unless configured. `name` must be unique per entry and not
    # collide with "workspace" (the other built-in volume name).
    worker_pvc_mounts: list[WorkerMount] = []

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
