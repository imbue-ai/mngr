from typing import Any

from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.client import OvhVpsClient


def resolve_image_id(client: OvhVpsClient, service_name: str, image_name: str) -> str:
    """Resolve a human image name (e.g. "Debian 12 - Docker") to its UUID.

    OVH image IDs are per-VPS and per-region UUIDs; the same OS template
    has different ids on different VPS models. The mapping comes from
    ``GET /vps/{s}/images/available/{id}`` for each id in
    ``GET /vps/{s}/images/available``.
    """
    image_ids = client.call_api("GET", f"/vps/{service_name}/images/available")
    if not isinstance(image_ids, list):
        raise MngrError(f"Unexpected response from /vps/{service_name}/images/available: {image_ids!r}")
    for image_id in image_ids:
        info = client.call_api("GET", f"/vps/{service_name}/images/available/{image_id}")
        if isinstance(info, dict) and str(info.get("name", "")) == image_name:
            return str(info["id"])
    raise MngrError(
        f"No OVH image named {image_name!r} available for VPS {service_name}. "
        f"Run ``mngr exec`` against the VPS to inspect /vps/{service_name}/images/available."
    )


def list_available_image_names(client: OvhVpsClient, service_name: str) -> list[str]:
    """Return the human-readable image names available for a specific VPS."""
    image_ids = client.call_api("GET", f"/vps/{service_name}/images/available")
    if not isinstance(image_ids, list):
        return []
    names: list[str] = []
    for image_id in image_ids:
        info = client.call_api("GET", f"/vps/{service_name}/images/available/{image_id}")
        if isinstance(info, dict) and "name" in info:
            names.append(str(info["name"]))
    return names


def validate_datacenter(allowed: list[str], datacenter: str) -> str:
    """Confirm a datacenter code is in the order's allowed list.

    The OVH order ``requiredConfiguration`` endpoint reports a list of
    valid datacenter codes per plan; callers pass that list here so we can
    error out before charging anyone.
    """
    if datacenter not in allowed:
        raise MngrError(
            f"OVH datacenter {datacenter!r} is not available for this plan; valid options: {sorted(allowed)}"
        )
    return datacenter


def find_required_field(required_config: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """Return the entry for ``label`` from ``requiredConfiguration`` output."""
    for field in required_config:
        if field.get("label") == label:
            return field
    raise MngrError(
        f"OVH order required-configuration field {label!r} not present; "
        f"got labels: {[f.get('label') for f in required_config]}"
    )
