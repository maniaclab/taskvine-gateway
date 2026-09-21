from kubernetes import client
from kubernetes.client.rest import ApiException

from .config import settings
from .slugs import label_value_slug, object_name_slug

# {username} in a naming/mount template is substituted with this - matches
# KubeSpawner's own {username} template scheme exactly (safe_slug with
# max_length=48, or escape_slug - see settings.username_slug_scheme), so a
# PVC name or mount path built here lines up with whatever KubeSpawner's
# own pre_spawn_hook (or extraVolumes/extraVolumeMounts {username}
# templating) actually created for the same user.
_OBJECT_NAME_SLUG_MAX_LENGTH = 48

# Holds a k8s-safe slug of the caller's username (see slugs.py), not the
# raw username itself - label values have their own character
# restrictions a raw username can violate. The raw username is preserved
# separately in USERNAME_ANNOTATION (annotations aren't restricted the
# same way) for anything that needs to display or recompute names from
# it - same split KubeSpawner itself uses for its own username label vs.
# annotation.
USER_LABEL = "taskvine-gateway/user"
USERNAME_ANNOTATION = "taskvine-gateway/username"

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
    """`username` is always the caller's raw Hub username - the slug
    substitution happens here, not at call sites, so this stays a pure
    function of the raw username no matter where it's called from."""
    slug = object_name_slug(username, _OBJECT_NAME_SLUG_MAX_LENGTH, settings.username_slug_scheme)
    return settings.manager_service_name_template.format(username=slug)


def worker_statefulset_name(username: str) -> str:
    """See manager_service_name - same rule, `username` is always raw."""
    slug = object_name_slug(username, _OBJECT_NAME_SLUG_MAX_LENGTH, settings.username_slug_scheme)
    return settings.worker_statefulset_name_template.format(username=slug)


def _pvc_mount_volume_and_mount(mount, username: str) -> tuple[client.V1Volume, client.V1VolumeMount]:
    # `username` is raw; slugged the same way KubeSpawner slugs its own
    # {username} templates (e.g. in a pre_spawn_hook or
    # extraVolumes/extraVolumeMounts), so a per-user claim_name_template/
    # mount_path_template here resolves to the exact same PVC/path
    # KubeSpawner already created for this user.
    slug = object_name_slug(username, _OBJECT_NAME_SLUG_MAX_LENGTH, settings.username_slug_scheme)
    volume = client.V1Volume(
        name=mount.name,
        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
            claim_name=mount.claim_name_template.format(username=slug)
        ),
    )
    volume_mount = client.V1VolumeMount(
        name=mount.name,
        mount_path=mount.mount_path_template.format(username=slug),
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

    # The selector must match KubeSpawner's own hub.jupyter.org/username
    # *label* value on the real notebook pod - which is a slug of the raw
    # username (see slugs.py), not the raw username itself. Using the raw
    # username here would silently select zero pods (or, for a username
    # already invalid as a label value, fail Service creation outright).
    username_label = label_value_slug(username, scheme=settings.username_slug_scheme)
    body = client.V1Service(
        metadata=client.V1ObjectMeta(name=name, labels={USER_LABEL: username_label}),
        spec=client.V1ServiceSpec(
            selector={"hub.jupyter.org/username": username_label},
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
    # USER_LABEL holds a slug (see the comment on it) - USERNAME_ANNOTATION
    # keeps the real raw username around for resolved_config/PoolStatus to
    # display, and for the idle reaper to recompute names from (annotation
    # values aren't restricted the way label values are, so the raw
    # username - which can contain "@" etc - is safe to store as-is here).
    labels = {"app": settings.worker_app_label, USER_LABEL: label_value_slug(username, scheme=settings.username_slug_scheme)}
    # workspace_kind is always the deployment's own setting - never a
    # per-request value, see ScaleRequest in models.py for why.
    workspace_kind = settings.worker_workspace_kind
    annotations = {
        USERNAME_ANNOTATION: username,
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
            service_name=settings.worker_app_label,
            replicas=replicas,
            selector=client.V1LabelSelector(match_labels=labels),
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
