"""Friendly display labels for mngr provider instance names in the workspace listing."""

from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import backend_for_connection_instance_name
from imbue.minds.desktop_client.connections.primitives import connection_backend_from_name

_AWS_PROVIDER_PREFIX: Final[str] = "aws"
_IMBUE_CLOUD_PROVIDER_PREFIX: Final[str] = "imbue_cloud"

# The label every connection instance (``<backend>_<hex>``) of a backend collapses to.
_CONNECTION_BACKEND_LABELS: Final[dict[ConnectionBackend, str]] = {
    ConnectionBackend.AWS: "AWS",
    ConnectionBackend.GCP: "GCP",
    ConnectionBackend.AZURE: "Azure",
    ConnectionBackend.MODAL: "Modal",
}

# Exact provider-instance names that map to a fixed display label. The aws
# backend name (and the retired per-region ``aws-<region>`` spelling a synced
# record may still carry) and the per-account imbue_cloud instances
# (``imbue_cloud_<user id>``) are handled by prefix below, not here, so they
# collapse to a single label regardless of region/account.
_EXACT_PROVIDER_LABELS: Final[dict[str, str]] = {
    "docker": "Docker",
    "lima": "Lima",
    "vultr": "Vultr",
    "ovh": "OVH",
    "modal": "Modal",
}


@pure
def connection_backend_label(backend: ConnectionBackend) -> str:
    """The display label of a connection backend (the one every ``<backend>_<hex>`` instance collapses to)."""
    return _CONNECTION_BACKEND_LABELS[backend]


@pure
def connection_backend_label_for_name(backend_name: str) -> str:
    """The display label for a backend name from a connection's metadata; the raw name for one this app does not know."""
    backend = connection_backend_from_name(backend_name)
    return backend_name if backend is None else _CONNECTION_BACKEND_LABELS[backend]


@pure
def friendly_provider_label(provider_name: str | None) -> str:
    """Map an mngr provider instance name to a human-readable compute-provider label.

    Collapses every connection instance (``<backend>_<hex>``) to its backend's
    label, the bare ``aws`` backend (and the retired ``aws-<region>`` spelling a
    synced record may still carry) to "AWS", and every per-account imbue_cloud
    provider (``imbue_cloud_<user id>``, or the legacy email-slug spelling) to
    "Imbue Cloud", so the workspace listing shows one stable label per compute
    provider regardless of region/account. Returns "" for an unknown/None
    provider (the row then shows no provider chip), and falls back to the raw
    name for any provider not in the known set so a newly-added provider is
    still visible.
    """
    if not provider_name:
        return ""
    connection_backend = backend_for_connection_instance_name(provider_name)
    if connection_backend is not None:
        # Connection instances (``<backend>_<hex>``) collapse to their backend;
        # the alias lives in the connection store, which this pure helper
        # deliberately does not consult.
        return _CONNECTION_BACKEND_LABELS[connection_backend]
    # CLEANUP: the ``aws-`` prefix half can go once no synced workspace record
    # carries a per-region ``aws-<region>`` provider (records predating connections).
    if provider_name == _AWS_PROVIDER_PREFIX or provider_name.startswith(f"{_AWS_PROVIDER_PREFIX}-"):
        return "AWS"
    if is_imbue_cloud_provider_name(provider_name):
        return "Imbue Cloud"
    return _EXACT_PROVIDER_LABELS.get(provider_name, provider_name)


def is_imbue_cloud_provider_name(provider_name: str) -> bool:
    """Whether a provider instance name is one of the imbue_cloud (remote workspace) providers."""
    return provider_name == _IMBUE_CLOUD_PROVIDER_PREFIX or provider_name.startswith(
        f"{_IMBUE_CLOUD_PROVIDER_PREFIX}_"
    )
