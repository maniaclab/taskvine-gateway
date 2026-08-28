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

# Stamped onto every worker StatefulSet with the resolved (default or
# per-pool-overridden) config it was created/last updated with - lets
# get_worker_pool/PoolStatus report the pool's actual config back without
# re-deriving it from container args, and lets ensure_worker_pool detect a
# request to change workspace_size_gb (or a deployment-wide change to
# worker_workspace_kind - not user-settable, see ScaleRequest in models.py,
# but still fixed once a pool exists) on an existing pool.
CORES_ANNOTATION = "taskvine-gateway/cores"
MEMORY_MB_ANNOTATION = "taskvine-gateway/memory-mb"
WORKSPACE_KIND_ANNOTATION = "taskvine-gateway/workspace-kind"
WORKSPACE_SIZE_GB_ANNOTATION = "taskvine-gateway/workspace-size-gb"

# vine_worker's advertised disk is derived from workspace_size_gb, not
# independently stored/settable - see the comment on config.py's
# worker_cores for why.
_MB_PER_GB = 1024


def _disk_mb_for(workspace_size_gb: int) -> int:
    return workspace_size_gb * _MB_PER_GB


class ScaleValidationError(ValueError):
    """Raised for a ScaleRequest value that's out of bounds - main.py turns
    this into a 422."""


class WorkspaceImmutableError(ValueError):
    """Raised when a ScaleRequest's workspace_size_gb (or a
    deployment-wide change to worker_workspace_kind) would change the
    workspace volume of a pool that already exists - a StatefulSet's
    volumeClaimTemplates are immutable in Kubernetes once created, so this
    can't be applied in place. main.py turns this into a 409."""


def manager_service_name(username: str) -> str:
    return settings.manager_service_name_template.format(username=username)


def worker_statefulset_name(username: str) -> str:
    return settings.worker_statefulset_name_template.format(username=username)


def _pvc_mount_volume_and_mount(mount, username: str) -> tuple[client.V1Volume, client.V1VolumeMount]:
    volume = client.V1Volume(
        name=mount.name,
        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
            claim_name=mount.claim_name_template.format(username=username)
        ),
    )
    volume_mount = client.V1VolumeMount(
        name=mount.name,
        mount_path=mount.mount_path_template.format(username=username),
        read_only=mount.read_only,
    )
    return volume, volume_mount


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


def _resolve_and_validate(
    replicas: int,
    cores: int | None,
    memory_mb: int | None,
    workspace_size_gb: int | None,
) -> tuple[int, int, int, int]:
    """Resolve None fields to their deployment default, check each
    per-worker value and the pool-wide replicas*cores/replicas*memory_mb
    totals against their ceilings, and derive disk_mb from
    workspace_size_gb. Returns (cores, memory_mb, disk_mb, workspace_size_gb).

    workspace_kind isn't handled here - it's not a per-request value at
    all (see ScaleRequest in models.py), so callers just read
    settings.worker_workspace_kind directly.
    """
    cores = cores if cores is not None else settings.worker_cores
    memory_mb = memory_mb if memory_mb is not None else settings.worker_memory_mb
    workspace_size_gb = workspace_size_gb if workspace_size_gb is not None else settings.worker_workspace_size_gb

    if cores > settings.max_worker_cores:
        raise ScaleValidationError(f"cores={cores} exceeds the per-worker maximum of {settings.max_worker_cores}")
    if memory_mb > settings.max_worker_memory_mb:
        raise ScaleValidationError(
            f"memory_mb={memory_mb} exceeds the per-worker maximum of {settings.max_worker_memory_mb}"
        )
    if workspace_size_gb > settings.max_worker_workspace_size_gb:
        raise ScaleValidationError(
            f"workspace_size_gb={workspace_size_gb} exceeds the per-worker maximum of {settings.max_worker_workspace_size_gb}"
        )

    pool_cores = replicas * cores
    pool_memory_mb = replicas * memory_mb
    if pool_cores > settings.max_pool_cores:
        raise ScaleValidationError(
            f"replicas * cores = {replicas} * {cores} = {pool_cores} exceeds the pool maximum of {settings.max_pool_cores} cores"
        )
    if pool_memory_mb > settings.max_pool_memory_mb:
        raise ScaleValidationError(
            f"replicas * memory_mb = {replicas} * {memory_mb} = {pool_memory_mb} "
            f"exceeds the pool maximum of {settings.max_pool_memory_mb} MB"
        )

    return cores, memory_mb, _disk_mb_for(workspace_size_gb), workspace_size_gb


def resolved_config(statefulset: client.V1StatefulSet) -> dict:
    """Read a pool's resolved cores/memory/disk/workspace config back from
    the annotations _worker_statefulset_body stamped on it. Falls back to
    the deployment's current defaults for a pool created before this
    annotation existed, rather than raising - best-effort for display, not
    load-bearing for anything else."""
    annotations = statefulset.metadata.annotations or {}
    workspace_size_gb = int(annotations.get(WORKSPACE_SIZE_GB_ANNOTATION, settings.worker_workspace_size_gb))
    return {
        "cores": int(annotations.get(CORES_ANNOTATION, settings.worker_cores)),
        "memory_mb": int(annotations.get(MEMORY_MB_ANNOTATION, settings.worker_memory_mb)),
        "disk_mb": _disk_mb_for(workspace_size_gb),
        "workspace_kind": annotations.get(WORKSPACE_KIND_ANNOTATION, settings.worker_workspace_kind),
        "workspace_size_gb": workspace_size_gb,
    }


def _worker_statefulset_body(
    username: str,
    replicas: int,
    cores: int,
    memory_mb: int,
    disk_mb: int,
    workspace_size_gb: int,
) -> client.V1StatefulSet:
    name = worker_statefulset_name(username)
    manager_host = f"{manager_service_name(username)}.{settings.namespace}.svc.cluster.local"
    labels = {"app": WORKER_APP_LABEL, USER_LABEL: username}
    # workspace_kind is always the deployment's own setting - never a
    # per-request value, see ScaleRequest in models.py for why.
    workspace_kind = settings.worker_workspace_kind
    annotations = {
        CORES_ANNOTATION: str(cores),
        MEMORY_MB_ANNOTATION: str(memory_mb),
        WORKSPACE_KIND_ANNOTATION: workspace_kind,
        WORKSPACE_SIZE_GB_ANNOTATION: str(workspace_size_gb),
    }

    pvc_mounts = [_pvc_mount_volume_and_mount(m, username) for m in settings.worker_pvc_mounts]

    # ndcctools is baked into worker_image at build time (see worker/Dockerfile
    # in this repo) - no initContainer/install step needed at pod start, just
    # exec vine_worker directly with its own CLI args.
    vine_worker = client.V1Container(
        name="vine-worker",
        image=settings.worker_image,
        args=[
            f"--cores={cores}",
            f"--memory={memory_mb}",
            f"--disk={disk_mb}",
            "--connect-timeout=900",
            "--idle-timeout=86400",
            manager_host,
            str(settings.manager_port),
        ],
        working_dir="/workspace",
        resources=client.V1ResourceRequirements(
            requests={"cpu": str(cores), "memory": f"{memory_mb}Mi"},
            limits={"cpu": str(cores), "memory": f"{memory_mb}Mi"},
        ),
        volume_mounts=[
            client.V1VolumeMount(name="workspace", mount_path="/workspace"),
            *(vm for _, vm in pvc_mounts),
        ],
    )

    volumes = [v for v, _ in pvc_mounts]

    volume_claim_templates = None
    if workspace_kind == "emptydir":
        volumes.append(
            client.V1Volume(
                name="workspace",
                empty_dir=client.V1EmptyDirVolumeSource(size_limit=f"{workspace_size_gb}Gi"),
            )
        )
    else:
        volume_claim_templates = [
            client.V1PersistentVolumeClaim(
                metadata=client.V1ObjectMeta(name="workspace"),
                spec=client.V1PersistentVolumeClaimSpec(
                    access_modes=["ReadWriteOnce"],
                    storage_class_name=settings.worker_workspace_storage_class,
                    resources=client.V1ResourceRequirements(requests={"storage": f"{workspace_size_gb}Gi"}),
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
        metadata=client.V1ObjectMeta(name=name, labels=labels, annotations=annotations),
        spec=client.V1StatefulSetSpec(
            service_name=WORKER_APP_LABEL,
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels={"app": WORKER_APP_LABEL, USER_LABEL: username}),
            template=client.V1PodTemplateSpec(metadata=client.V1ObjectMeta(labels=labels), spec=pod_spec),
            volume_claim_templates=volume_claim_templates,
        ),
    )


def ensure_worker_pool(
    apps_api: client.AppsV1Api,
    core_api: client.CoreV1Api,
    username: str,
    replicas: int,
    *,
    cores: int | None = None,
    memory_mb: int | None = None,
    workspace_size_gb: int | None = None,
) -> client.V1StatefulSet:
    if not 0 <= replicas <= settings.max_workers_per_user:
        raise ScaleValidationError(f"replicas must be between 0 and {settings.max_workers_per_user}")

    cores, memory_mb, disk_mb, workspace_size_gb = _resolve_and_validate(replicas, cores, memory_mb, workspace_size_gb)

    ensure_manager_service(core_api, username)

    name = worker_statefulset_name(username)
    try:
        existing = apps_api.read_namespaced_stateful_set(name, settings.namespace)
    except ApiException as e:
        if e.status != 404:
            raise
        existing = None

    if existing is not None:
        existing_config = resolved_config(existing)
        # workspace_kind can only differ here if the deployment's own
        # worker_workspace_kind setting changed since this pool was
        # created - it's never user-settable (see ScaleRequest) - but the
        # StatefulSet's volumeClaimTemplates would still reject an attempt
        # to apply that change in place, so this still needs to be caught.
        if existing_config["workspace_kind"] != settings.worker_workspace_kind:
            raise WorkspaceImmutableError(
                f"this pool's workspace_kind is fixed at {existing_config['workspace_kind']!r} since creation "
                f"(the deployment's current setting is {settings.worker_workspace_kind!r}) - "
                "delete the pool first (DELETE /pools/me) to change it"
            )
        if existing_config["workspace_size_gb"] != workspace_size_gb:
            raise WorkspaceImmutableError(
                f"this pool's workspace_size_gb is fixed at {existing_config['workspace_size_gb']} since creation "
                f"(requested {workspace_size_gb}) - delete the pool first (DELETE /pools/me) to change it"
            )

    body = _worker_statefulset_body(username, replicas, cores, memory_mb, disk_mb, workspace_size_gb)

    if existing is not None:
        try:
            return apps_api.patch_namespaced_stateful_set(name, settings.namespace, body)
        except ApiException as e:
            # Defensive fallback for a pool that predates the annotations
            # above (so the check up top couldn't catch a real
            # workspace_kind/size change) - the k8s API itself rejects an
            # attempted change to volumeClaimTemplates as invalid.
            if e.status in (400, 422):
                raise WorkspaceImmutableError(
                    "this pool's workspace_kind/workspace_size_gb can't be changed in place - "
                    "delete the pool first (DELETE /pools/me) to change it"
                ) from e
            raise

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
