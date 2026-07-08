"""Friendly display labels for mngr provider instance names in the workspace listing."""

from typing import Final

from imbue.imbue_common.pure import pure

_AWS_PROVIDER_PREFIX: Final[str] = "aws"
_IMBUE_CLOUD_PROVIDER_PREFIX: Final[str] = "imbue_cloud"

# Exact provider-instance names that map to a fixed display label. Per-region
# AWS (``aws-<region>``) and per-account imbue_cloud (``imbue_cloud_<slug>``)
# providers are handled by prefix below, not here, so they collapse to a single
# label regardless of region/account.
_EXACT_PROVIDER_LABELS: Final[dict[str, str]] = {
    "docker": "Docker",
    "lima": "Lima",
    "vultr": "Vultr",
    "ovh": "OVH",
    "modal": "Modal",
}


@pure
def friendly_provider_label(provider_name: str | None) -> str:
    """Map an mngr provider instance name to a human-readable compute-provider label.

    Collapses every per-region AWS provider (``aws-<region>``) to "AWS" and every
    per-account imbue_cloud provider (``imbue_cloud_<slug>``) to "Imbue Cloud" so
    the workspace listing shows one stable label per compute provider regardless
    of region/account. Returns "" for an unknown/None provider (the row then
    shows no provider chip), and falls back to the raw name for any provider not
    in the known set so a newly-added provider is still visible.
    """
    if not provider_name:
        return ""
    if provider_name == _AWS_PROVIDER_PREFIX or provider_name.startswith(f"{_AWS_PROVIDER_PREFIX}-"):
        return "AWS"
    if provider_name == _IMBUE_CLOUD_PROVIDER_PREFIX or provider_name.startswith(f"{_IMBUE_CLOUD_PROVIDER_PREFIX}_"):
        return "Imbue Cloud"
    return _EXACT_PROVIDER_LABELS.get(provider_name, provider_name)
