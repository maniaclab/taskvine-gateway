"""K8s-safe username slugging.

Thin wrapper around `escapism <https://github.com/jupyterhub/escapism>`_ -
the same package JupyterHub's KubeSpawner itself uses for this - rather
than reimplementing the escaping by hand, so any upstream fix or edge
case flows through as an ordinary dependency bump instead of silently
drifting out of sync.

Why this needs to exist at all: KubeSpawner never uses a user's raw Hub
username directly in a k8s object name or label value - it escapes it
first, since a raw username can contain characters invalid in either
(e.g. "jzhou24@nd.edu"). It does this for the singleuser pod's own
`hub.jupyter.org/username` label and for any `{username}`-templated
resource name it creates (e.g. a per-user PVC from a pre_spawn_hook).
This gateway has to reproduce that same escaping exactly, or the names/
labels/paths it computes don't match what KubeSpawner actually created -
see the k8s.py callers of object_name_slug/label_value_slug for exactly
where each is needed and why.

Two escaping schemes exist because KubeSpawner itself supports two,
selected by its own `c.KubeSpawner.slug_scheme` config (TVG_USERNAME_SLUG_SCHEME
here must match whatever that deployment's z2jh chart is actually
configured with - "safe" is KubeSpawner's current default; "escape" is
its older, still-supported scheme, documented upstream as legacy):

- "safe" (default): valid names are used as-is; anything else becomes a
  truncated, sanitized substring plus a short content hash, guaranteeing
  both validity and no collisions.
- "escape": every disallowed character is individually hex-escaped
  (e.g. "@" -> "-40"), a reversible but far longer encoding.
"""

from typing import Literal

import escapism

SlugScheme = Literal["safe", "escape"]

_escape_slug_safe_chars = set("abcdefghijklmnopqrstuvwxyz0123456789")


def _escape_slug(name: str) -> str:
    return escapism.escape(name, safe=_escape_slug_safe_chars, escape_char="-").lower()


def object_name_slug(name: str, max_length: int, scheme: SlugScheme = "safe") -> str:
    """Matches KubeSpawner's `{username}`/`{servername}` template
    substitution - what a KubeSpawner-templated PVC name or mount path
    actually resolves to for this username."""
    if scheme == "escape":
        return _escape_slug(name)
    return escapism.safe_slug(name, is_valid=escapism.is_valid_object_name, max_length=max_length)


def label_value_slug(name: str, max_length: int = 32, scheme: SlugScheme = "safe") -> str:
    """Matches KubeSpawner's `hub.jupyter.org/username` pod label value -
    what to put in a label selector to actually match the user's real
    notebook pod."""
    if scheme == "escape":
        return _escape_slug(name)
    return escapism.safe_slug(name, is_valid=escapism.is_valid_label, max_length=max_length)
