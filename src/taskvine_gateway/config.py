from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TVG_")

    # Namespace singleuser pods and workers live in on this cluster.
    namespace: str = "jupyterhub"

    # Worker pod image + how it resolves ndcctools (mirrors the hand-built
    # prototype in clusters/*/infrastructure/taskvine-workers).
    worker_image: str = "ghcr.io/prefix-dev/pixi:0.76.1"
    worker_pixi_configmap: str = "taskvine-worker-pixi"

    # Per-cluster storage classes - these differ across clusters (e.g.
    # odf-ceph-rbd vs iu-ceph-block) so must be set via env in the cluster
    # overlay, not left at these defaults.
    worker_workspace_storage_class: str = "REPLACE_ME"
    worker_workspace_storage_size: str = "5Gi"

    manager_port: int = 9123

    worker_cpu: str = "1"
    worker_memory: str = "2Gi"

    # Naming templates - {username} is substituted by str.format.
    manager_service_name_template: str = "taskvine-manager-{username}"
    worker_statefulset_name_template: str = "taskvine-worker-{username}"
    shared_data_pvc_template: str = "shared-data-{username}"

    max_workers_per_user: int = 20


settings = Settings()
