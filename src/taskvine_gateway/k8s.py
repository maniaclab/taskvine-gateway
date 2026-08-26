from kubernetes import client
from kubernetes.client.rest import ApiException

from .config import settings

# Every worker pod keeps `app: taskvine-worker` (in addition to the per-user
# label below) so the existing singleuser NetworkPolicy - which allows
# ingress to port 9123 from pods matching `app: taskvine-worker`
# (clusters/*/infrastructure/jupyterhub/install/patch-taskvine.yaml) - keeps
# working unmodified for gateway-created pools.
WORKER_APP_LABEL = "taskvine-worker"
USER_LABEL = "taskvine-gateway/user"


def manager_service_name(username: str) -> str:
    return settings.manager_service_name_template.format(username=username)


def worker_statefulset_name(username: str) -> str:
    return settings.worker_statefulset_name_template.format(username=username)


def shared_data_pvc_name(username: str) -> str:
    return settings.shared_data_pvc_template.format(username=username)


def shared_data_mount_path(username: str) -> str:
    return settings.shared_data_mount_path_template.format(username=username)


def ensure_manager_service(api: client.CoreV1Api, username: str) -> None:
    """Get-or-create the Service that gives a stable DNS name to a user's
    notebook pod (z2jh does not create one)."""
    name = manager_service_name(username)
    try:
        api.read_namespaced_service(name, settings.namespace)
        return
    except ApiException as e:
        if e.status != 404:
            raise

    body = client.V1Service(
        metadata=client.V1ObjectMeta(name=name, labels={USER_LABEL: username}),
        spec=client.V1ServiceSpec(
            selector={"hub.jupyter.org/username": username},
            ports=[client.V1ServicePort(name="taskvine-manager", port=settings.manager_port, target_port=settings.manager_port)],
        ),
    )
    try:
        api.create_namespaced_service(settings.namespace, body)
    except ApiException as e:
        if e.status != 409:
            raise


def _worker_statefulset_body(username: str, replicas: int) -> client.V1StatefulSet:
    name = worker_statefulset_name(username)
    manager_host = f"{manager_service_name(username)}.{settings.namespace}.svc.cluster.local"
    labels = {"app": WORKER_APP_LABEL, USER_LABEL: username}

    # ndcctools is baked into worker_image at build time (see worker/Dockerfile
    # in this repo) - no initContainer/install step needed at pod start, just
    # exec vine_worker directly with its own CLI args.
    vine_worker = client.V1Container(
        name="vine-worker",
        image=settings.worker_image,
        args=[
            f"--cores={settings.worker_cores}",
            f"--memory={settings.worker_memory_mb}",
            f"--disk={settings.worker_disk_mb}",
            "--connect-timeout=900",
            "--idle-timeout=86400",
            manager_host,
            str(settings.manager_port),
        ],
        working_dir="/workspace",
        resources=client.V1ResourceRequirements(
            requests={"cpu": str(settings.worker_cores), "memory": f"{settings.worker_memory_mb}Mi"},
            limits={"cpu": str(settings.worker_cores), "memory": f"{settings.worker_memory_mb}Mi"},
        ),
        volume_mounts=[
            client.V1VolumeMount(name="workspace", mount_path="/workspace"),
            client.V1VolumeMount(name="shared-data", mount_path=shared_data_mount_path(username)),
        ],
    )

    volumes = [
        client.V1Volume(
            name="shared-data",
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=shared_data_pvc_name(username)),
        ),
    ]

    volume_claim_templates = None
    if settings.worker_workspace_kind == "emptydir":
        volumes.append(
            client.V1Volume(
                name="workspace",
                empty_dir=client.V1EmptyDirVolumeSource(size_limit=settings.worker_workspace_size_limit),
            )
        )
    else:
        volume_claim_templates = [
            client.V1PersistentVolumeClaim(
                metadata=client.V1ObjectMeta(name="workspace"),
                spec=client.V1PersistentVolumeClaimSpec(
                    access_modes=["ReadWriteOnce"],
                    storage_class_name=settings.worker_workspace_storage_class,
                    resources=client.V1ResourceRequirements(requests={"storage": settings.worker_workspace_storage_size}),
                ),
            )
        ]

    pod_spec = client.V1PodSpec(
        containers=[vine_worker],
        volumes=volumes,
        image_pull_secrets=(
            [client.V1LocalObjectReference(name=settings.worker_image_pull_secret)]
            if settings.worker_image_pull_secret
            else None
        ),
    )

    return client.V1StatefulSet(
        metadata=client.V1ObjectMeta(name=name, labels=labels),
        spec=client.V1StatefulSetSpec(
            service_name=WORKER_APP_LABEL,
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels={"app": WORKER_APP_LABEL, USER_LABEL: username}),
            template=client.V1PodTemplateSpec(metadata=client.V1ObjectMeta(labels=labels), spec=pod_spec),
            volume_claim_templates=volume_claim_templates,
        ),
    )


def ensure_worker_pool(apps_api: client.AppsV1Api, core_api: client.CoreV1Api, username: str, replicas: int) -> client.V1StatefulSet:
    if not 0 <= replicas <= settings.max_workers_per_user:
        raise ValueError(f"replicas must be between 0 and {settings.max_workers_per_user}")

    ensure_manager_service(core_api, username)

    name = worker_statefulset_name(username)
    try:
        existing = apps_api.read_namespaced_stateful_set(name, settings.namespace)
        existing.spec.replicas = replicas
        return apps_api.patch_namespaced_stateful_set(name, settings.namespace, existing)
    except ApiException as e:
        if e.status != 404:
            raise

    body = _worker_statefulset_body(username, replicas)
    return apps_api.create_namespaced_stateful_set(settings.namespace, body)


def get_worker_pool(apps_api: client.AppsV1Api, username: str) -> client.V1StatefulSet | None:
    try:
        return apps_api.read_namespaced_stateful_set(worker_statefulset_name(username), settings.namespace)
    except ApiException as e:
        if e.status == 404:
            return None
        raise


def delete_worker_pool(apps_api: client.AppsV1Api, core_api: client.CoreV1Api, username: str) -> None:
    name = worker_statefulset_name(username)
    try:
        apps_api.delete_namespaced_stateful_set(name, settings.namespace, propagation_policy="Foreground")
    except ApiException as e:
        if e.status != 404:
            raise

    try:
        core_api.delete_namespaced_service(manager_service_name(username), settings.namespace)
    except ApiException as e:
        if e.status != 404:
            raise
