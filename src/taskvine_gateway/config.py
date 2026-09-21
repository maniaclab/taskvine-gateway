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

    # Must match this deployment's z2jh chart config
    # (hub.kubespawner.slug_scheme, or c.KubeSpawner.slug_scheme directly) -
    # KubeSpawner never uses a user's raw Hub username as a k8s object name
    # or label value, it escapes it first (see slugs.py), and this gateway
    # has to reproduce whichever scheme that deployment actually uses or
    # the names/labels/mount paths it computes won't match what KubeSpawner
    # created. "safe" is KubeSpawner's own current default; "escape" is its
    # older, still-supported scheme.
    username_slug_scheme: Literal["safe", "escape"] = "safe"

    # Purpose-built image with ndcctools baked in at build time - no
    # runtime install step, so no dependency on conda-forge being reachable
    # at pod start, and no initContainer at all. There's no generic public
    # default that works for every deployment - build your own from
    # worker/ in this repo (or extend it) and publish it somewhere your
    # cluster can pull from, then set this to that image.
    worker_image: str = "REPLACE_ME"

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
    # Both this and worker_workspace_size_gb are the *defaults* used when a
    # ScaleRequest doesn't override them - see max_worker_workspace_size_gb
    # below for the ceiling on the override.
    worker_workspace_kind: Literal["emptydir", "pvc"] = "emptydir"
    worker_workspace_size_gb: int = 5

    # Only used when workspace_kind == "pvc". Storage classes differ across
    # clusters (e.g. odf-ceph-rbd vs iu-ceph-block) so must be set via env
    # in the cluster overlay, not left at this default. Not overridable
    # per-pool - unlike kind/size, which storage class backs a PVC isn't a
    # per-user decision.
    worker_workspace_storage_class: str = "REPLACE_ME"

    manager_port: int = 9123

    # Single source of truth for a worker's cores/memory, in the units
    # vine_worker's own --cores/--memory flags expect (whole cores, MB).
    # The k8s container's resources.requests/limits are derived from these
    # same values (see k8s.py) so what's advertised to the manager always
    # matches what the pod is actually allocated - request == limit
    # (Guaranteed QoS) for the same reason dask-gateway's workers do here:
    # so a worker pod can't balloon past its declared footprint.
    #
    # There's no separate worker_disk_mb: vine_worker's --disk is derived
    # from worker_workspace_size_gb above (disk_mb = workspace_size_gb *
    # 1024) rather than being its own independent number. Keeping them
    # independent meant a worker could advertise more disk to the manager
    # than the real emptyDir/PVC backing /workspace actually had, so a task
    # the manager thought would fit could fail with a real ENOSPC instead
    # of a clean rejection up front.
    worker_cores: int = 1
    worker_memory_mb: int = 2000

    # Per-worker ceilings a ScaleRequest's cores/memory_mb/workspace_size_gb
    # overrides cannot exceed. A request exceeding one of these is rejected
    # (422) outright, not silently clamped down to the ceiling.
    max_worker_cores: int = 4
    max_worker_memory_mb: int = 8000
    max_worker_workspace_size_gb: int = 20

    # Pool-wide ceilings on replicas * cores and replicas * memory_mb -
    # mirrors dask-gateway's own cluster_max_cores/cluster_max_memory here,
    # which bound a whole Dask cluster's total footprint, not any one
    # worker's (that's what max_worker_cores/max_worker_memory_mb above are
    # for - a genuinely different bound). Defaults match this deployment's
    # dask-gateway config for consistency between the two. No pool-wide
    # ceiling on disk: unlike cores/memory, which draw from a shared
    # cluster capacity pool worth protecting in aggregate, each worker's
    # workspace volume is its own dedicated emptyDir/PVC, already bounded
    # per-worker by max_worker_workspace_size_gb.
    max_pool_cores: int = 16
    max_pool_memory_mb: int = 32000

    # Naming templates - {username} is substituted with a k8s-safe slug of
    # the caller's raw Hub username (see slugs.py, and username_slug_scheme
    # above) rather than the raw username itself, same as KubeSpawner does
    # for its own {username} templates - a raw username can contain
    # characters invalid in a k8s object name. That slug can be up to 48
    # chars, and a worker StatefulSet's pod hostname (name + "-" +
    # ordinal) must fit within the 63-char DNS label limit - keep these
    # prefixes short so long usernames still fit (worker: prefix + 48 +
    # "-<ordinal>" <= 63).
    manager_service_name_template: str = "tv-mgr-{username}"
    worker_statefulset_name_template: str = "tv-wk-{username}"

    # Every worker pod's `app` label, so a deployment can allow ingress to
    # the manager port from worker pods without matching on the per-user
    # label (see clusters/*/infrastructure/jupyterhub/install/patch-taskvine.yaml
    # in rp1-core for an example NetworkPolicy that does exactly this) -
    # change it if it collides with an existing label convention. Also
    # used as the worker StatefulSet's headless service_name.
    worker_app_label: str = "taskvine-worker"

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
