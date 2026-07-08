import re
from collections.abc import Mapping
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr_ovh._tag_keys import MNGR_HOST_ID_TAG_KEY as MNGR_HOST_ID_TAG_KEY
from imbue.mngr_ovh._tag_keys import MNGR_PROVIDER_TAG_KEY as MNGR_PROVIDER_TAG_KEY
from imbue.mngr_ovh._tag_keys import MNGR_RECYCLING_LOCK_TAG_KEY as MNGR_RECYCLING_LOCK_TAG_KEY
from imbue.mngr_ovh.client import OvhVpsClient

_VPS_RESOURCE_TYPE: Final[str] = "vps"

# OVH IAM v2 tag keys must match this shape. OVH's documented contract is
# 1-49 chars, starting with a lowercase letter, then lowercase letters,
# digits, ``_`` and ``-``. We validate locally so that a typo'd
# MNGR_VPS_EXTRA_TAGS key fails *before* we order a VPS rather than after
# (the IAM tag attach happens at the end of provisioning, so a 400 there
# leaks a freshly-ordered month of billing).
_IAM_TAG_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_-]{0,48}$")

# Tag keys reserved by mngr internals. ``mngr-provider`` / ``mngr-host-id``
# back the cross-process discovery path; ``mngr-recycling-by`` is the
# cooperative recycle lock. All three match :data:`_IAM_TAG_KEY_PATTERN`,
# so without an explicit reservation an operator could silently overwrite
# them via ``MNGR_VPS_EXTRA_TAGS`` (e.g. break ``mngr list`` by retagging
# the provider, or hijack the recycle handshake). Reject the keys at
# parse time with a clear error.
_RESERVED_TAG_KEYS: Final[frozenset[str]] = frozenset(
    {MNGR_PROVIDER_TAG_KEY, MNGR_HOST_ID_TAG_KEY, MNGR_RECYCLING_LOCK_TAG_KEY}
)


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


def iam_region_code_for_endpoint(endpoint: str) -> str:
    """Map a python-ovh endpoint id (``ovh-us``) to the URN's region segment (``us``).

    Recognises the ``ovh-*`` family (which is what mngr's OVH backend
    supports). Raises ``MngrError`` for unrecognised endpoints rather
    than silently defaulting; a wrong URN region segment makes IAM v2
    tag operations target a non-existent resource, which would surface
    as a confusing 404 deep inside the recycle path.
    """
    if endpoint.startswith("ovh-"):
        return endpoint.removeprefix("ovh-")
    raise MngrError(
        f"Cannot derive IAM URN region from OVH endpoint {endpoint!r}; "
        "expected an ``ovh-*`` endpoint id (e.g. 'ovh-us', 'ovh-eu', 'ovh-ca')."
    )


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


def parse_extra_tags_env(raw: str) -> dict[str, str]:
    """Parse the ``MNGR_VPS_EXTRA_TAGS`` env var into a tag dict.

    Mirrors ``mngr_vps.build_vps_tags``'s splitting contract
    (comma-separated, strip whitespace, drop blank entries) but enforces
    OVH IAM v2 semantics:

    - Each entry must be ``key=value`` (one ``=``; surrounding whitespace
      around the key / value is stripped). Entries without ``=`` raise
      :class:`MngrError`.
    - Keys must match :data:`_IAM_TAG_KEY_PATTERN`. Mismatches raise
      :class:`MngrError`, surfacing the offending key. This catches typos
      (uppercase, leading digit, illegal symbol) before any API call --
      the alternative is an OVH 400 deep in the IAM attach loop, after
      we've already ordered the VPS.
    - Keys in :data:`_RESERVED_TAG_KEYS` (``mngr-provider``, ``mngr-host-id``,
      ``mngr-recycling-by``) are rejected because the OVH provider uses
      them as the discovery / recycle-lock tags. Letting a caller overwrite
      them silently breaks ``mngr list`` (a retagged ``mngr-provider``
      hides the VPS from the owning provider) and could hijack the
      cooperative recycle handshake.

    Empty / whitespace-only input is treated as no extra tags.
    """
    tags: dict[str, str] = {}
    for entry in raw.split(","):
        stripped = entry.strip()
        if not stripped:
            continue
        if "=" not in stripped:
            raise MngrError(f"MNGR_VPS_EXTRA_TAGS entry {stripped!r} is missing '='; each tag must be KEY=VALUE.")
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if not _IAM_TAG_KEY_PATTERN.fullmatch(key):
            raise MngrError(
                f"MNGR_VPS_EXTRA_TAGS key {key!r} is not a valid OVH IAM tag key. "
                f"Expected pattern {_IAM_TAG_KEY_PATTERN.pattern} "
                "(1-49 chars, starting with a lowercase letter, then [a-z0-9_-])."
            )
        if key in _RESERVED_TAG_KEYS:
            raise MngrError(
                f"MNGR_VPS_EXTRA_TAGS key {key!r} is reserved by mngr internals "
                f"(reserved keys: {sorted(_RESERVED_TAG_KEYS)}). Choose a different "
                "tag key."
            )
        tags[key] = value
    return tags


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
