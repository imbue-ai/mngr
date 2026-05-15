"""Find and delete Vultr instances belonging to a dynamic dev env.

Dev envs share the dev-tier Vultr API key, so the per-instance attribution
happens via a tag: every instance the pool-host bake creates for a dev
env carries ``minds_dev_env=<dev-name>`` in its ``tags`` array. ``minds env
destroy`` walks that list to enumerate / tear down everything its env owns.

There is no ``create`` operation here -- pool hosts are still provisioned
via the existing ``mngr imbue_cloud admin pool create`` flow; this module
only handles discovery + destruction.
"""

from typing import Final

import httpx
from pydantic import Field
from pydantic import SecretStr
from pydantic import TypeAdapter
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError

_VULTR_API_BASE: Final[str] = "https://api.vultr.com/v2"
_REQUEST_TIMEOUT_SECONDS: Final[float] = 60.0
_TAG_KEY: Final[str] = "minds_dev_env"


class VultrProviderError(MindError):
    """Raised when the Vultr API rejects a request."""


class VultrInstanceSummary(FrozenModel):
    """One row of ``GET /instances``.

    Vultr returns ~40 fields per instance (plan, region, OS, status, ...);
    we only care about the four below, so we tell pydantic to drop the
    rest rather than fail validation on every new Vultr API field.
    """

    model_config = {"extra": "ignore", "frozen": True}

    id: str
    label: str = ""
    main_ip: str = ""
    tags: tuple[str, ...] = Field(default=(), description="Vultr tag list verbatim.")


_INSTANCE_LIST_ADAPTER: TypeAdapter[list[VultrInstanceSummary]] = TypeAdapter(list[VultrInstanceSummary])


def _vultr_request(
    method: str,
    path: str,
    *,
    api_key: SecretStr,
    json_body: dict | None = None,
) -> dict | None:
    headers = {
        "Authorization": f"Bearer {api_key.get_secret_value()}",
        "Accept": "application/json",
    }
    url = f"{_VULTR_API_BASE}{path}"
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            response = client.request(method, url, headers=headers, json=json_body)
    except httpx.HTTPError as exc:
        raise VultrProviderError(f"Vultr API request failed ({method} {url}): {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise VultrProviderError(
            f"Vultr API returned {response.status_code} for {method} {url}: {response.text[:500]}"
        )
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise VultrProviderError(f"Vultr API returned non-JSON for {method} {url}: {exc}") from exc


def dev_env_tag(name: DevEnvName) -> str:
    """Return the tag string applied to Vultr instances owned by ``name``."""
    return f"{_TAG_KEY}={name}"


def list_dev_env_instances(name: DevEnvName, *, api_key: SecretStr) -> tuple[VultrInstanceSummary, ...]:
    """Return every Vultr instance carrying this dev env's tag.

    Walks the paginated ``/instances`` endpoint and filters client-side
    rather than relying on the ``tag`` query param (Vultr's filter is a
    substring match on the tag prefix, which can return false positives
    for similarly-named dev envs).
    """
    expected_tag = dev_env_tag(name)
    matches: list[VultrInstanceSummary] = []
    cursor: str | None = None
    has_more = True
    while has_more:
        path = "/instances?per_page=100"
        if cursor:
            path += f"&cursor={cursor}"
        payload = _vultr_request("GET", path, api_key=api_key)
        if payload is None:
            has_more = False
            continue
        instances_raw = payload.get("instances", [])
        try:
            instances = _INSTANCE_LIST_ADAPTER.validate_python(instances_raw)
        except ValidationError as exc:
            raise VultrProviderError(f"Vultr /instances returned an unexpected shape: {exc}") from exc
        for instance in instances:
            if expected_tag in instance.tags:
                matches.append(instance)
        meta = payload.get("meta") if isinstance(payload, dict) else None
        links = meta.get("links") if isinstance(meta, dict) else None
        cursor = links.get("next") if isinstance(links, dict) else None
        has_more = bool(cursor)
    return tuple(matches)


def delete_instances(instances: tuple[VultrInstanceSummary, ...], *, api_key: SecretStr) -> None:
    """Delete every Vultr instance in ``instances`` via ``DELETE /instances/{id}``.

    Failures on any single instance abort the loop (callers can re-run
    ``minds env destroy`` to retry). Returns silently when ``instances``
    is empty.
    """
    for instance in instances:
        _vultr_request("DELETE", f"/instances/{instance.id}", api_key=api_key)
