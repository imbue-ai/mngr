from collections.abc import Mapping
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr_ovh.client import OvhVpsClient

MNGR_PROVIDER_TAG_KEY: Final[str] = "mngr-provider"
MNGR_HOST_ID_TAG_KEY: Final[str] = "mngr-host-id"
MNGR_RECYCLING_LOCK_TAG_KEY: Final[str] = "mngr-recycling-by"

_VPS_RESOURCE_TYPE: Final[str] = "vps"


class IamResource(FrozenModel):
    """Minimal subset of OVH IAM v2 ``/iam/resource`` response we care about."""

    urn: str = Field(description="Universal Resource Name like urn:v1:us:resource:vps:<serviceName>")
    name: str = Field(description="OVH resource name (e.g. vps service name)")
    display_name: str = Field(default="", description="Human-set display name")
    type: str = Field(description="OVH resource type, e.g. 'vps' or 'publicCloudProject'")
    tags: Mapping[str, str] = Field(default_factory=dict, description="Resource tags (key/value)")


def vps_urn_for(service_name: str, *, region_code: str = "us") -> str:
    """Build the IAM resource URN for an OVH VPS owned by this account."""
    return f"urn:v1:{region_code}:resource:vps:{service_name}"


def attach_tag(
    client: OvhVpsClient,
    urn: str,
    key: str,
    value: str,
) -> None:
    """``POST /v2/iam/resource/{urn}/tag`` -- attach (or overwrite) a single tag."""
    client.call_api("POST", f"/v2/iam/resource/{urn}/tag", key=key, value=value)


def attach_tags(
    client: OvhVpsClient,
    urn: str,
    tags: Mapping[str, str],
) -> None:
    """Attach multiple tags by issuing one POST per pair (no bulk endpoint)."""
    for key, value in tags.items():
        attach_tag(client, urn, key, value)


def delete_tag(client: OvhVpsClient, urn: str, key: str) -> None:
    """``DELETE /v2/iam/resource/{urn}/tag/{key}``."""
    client.call_api("DELETE", f"/v2/iam/resource/{urn}/tag/{key}")


def list_vps_resources(client: OvhVpsClient) -> list[IamResource]:
    """List every IAM resource of type ``vps`` and return their tags.

    OVH's server-side ``?tags[k][value]=v`` filter is rejected as a bad
    request (verified live), so callers must filter by tags client-side.
    """
    payload = client.call_api("GET", f"/v2/iam/resource?resourceType={_VPS_RESOURCE_TYPE}")
    if not isinstance(payload, list):
        return []
    out: list[IamResource] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        try:
            out.append(_iam_resource_from_payload(raw))
        except MngrError as e:
            logger.warning("Skipping malformed IAM resource payload: {} -- {}", e, raw)
    return out


def list_vps_resources_for_provider(
    client: OvhVpsClient,
    provider_name: str,
) -> list[IamResource]:
    """Return only VPSes whose ``mngr-provider`` tag matches ``provider_name``."""
    return [r for r in list_vps_resources(client) if r.tags.get(MNGR_PROVIDER_TAG_KEY) == provider_name]


def get_vps_resource(client: OvhVpsClient, urn: str) -> IamResource | None:
    """Return the IAM resource record for a single VPS URN, or None if absent.

    Used by the recycle path to re-read tags after attempting to acquire a
    cooperative lock: if our lock UUID is no longer the unique recycler,
    another process beat us and we must back off.
    """
    for r in list_vps_resources(client):
        if r.urn == urn:
            return r
    return None


def _iam_resource_from_payload(raw: dict[str, Any]) -> IamResource:
    urn = str(raw.get("urn") or "")
    if not urn:
        raise MngrError(f"IAM resource payload missing 'urn': {raw!r}")
    return IamResource(
        urn=urn,
        name=str(raw.get("name") or ""),
        display_name=str(raw.get("displayName") or ""),
        type=str(raw.get("type") or ""),
        tags={str(k): str(v) for k, v in (raw.get("tags") or {}).items()},
    )
