"""List + delete Cloudflare tunnels belonging to a minds env.

Tunnels are created by the deployed ``remote_service_connector`` per
user request (``POST /tunnels``); each carries an ``env=<env-name>``
key in its Cloudflare-side ``metadata`` blob (set by
``cf_create_tunnel`` in the connector at create time). ``minds env
destroy`` reads that metadata to enumerate and delete every tunnel
belonging to the env -- without it, the tier's Cloudflare account
would leak orphan tunnels every time an env is destroyed.

Authentication uses the Cloudflare API token + account id from the
``cloudflare`` Vault entry (``CLOUDFLARE_API_TOKEN`` /
``CLOUDFLARE_ACCOUNT_ID``), which the operator already populates as
part of bringing up the tier. No connector contact required -- this
talks straight to the Cloudflare REST API.
"""

from typing import Final

import httpx
from pydantic import SecretStr

from imbue.minds.envs.primitives import DevEnvName
from imbue.minds.errors import MindError

_CF_API_BASE: Final[str] = "https://api.cloudflare.com/client/v4"
_REQUEST_TIMEOUT_SECONDS: Final[float] = 60.0
_TUNNELS_PER_PAGE: Final[int] = 50


class CloudflareTunnelProviderError(MindError):
    """Raised when the Cloudflare API rejects a tunnels list / delete request."""


def list_tunnels_for_env(
    name: DevEnvName,
    *,
    account_id: str,
    api_token: SecretStr,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, ...]:
    """Return every Cloudflare tunnel uuid whose metadata.env equals ``name``.

    Walks the paginated ``GET /accounts/<id>/cfd_tunnel`` endpoint with
    ``is_deleted=false`` (Cloudflare keeps deleted tunnels in the list
    by default), filters client-side on ``metadata.env`` since the API
    does not expose a metadata-filter query param.

    Returns an empty tuple when no tunnels match; surfaces any 4xx/5xx
    or shape mismatch as :class:`CloudflareTunnelProviderError` so
    destroy aborts rather than silently leaving tunnels around.
    """
    expected_env = str(name)
    headers = {
        "Authorization": f"Bearer {api_token.get_secret_value()}",
        "Accept": "application/json",
    }
    matches: list[str] = []
    page = 1
    has_more_pages = True
    while has_more_pages:
        url = f"{_CF_API_BASE}/accounts/{account_id}/cfd_tunnel"
        params = {"is_deleted": "false", "per_page": str(_TUNNELS_PER_PAGE), "page": str(page)}
        try:
            with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, transport=transport) as client:
                response = client.get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise CloudflareTunnelProviderError(f"Cloudflare API request failed (GET {url}): {exc}") from exc
        if response.status_code >= 400:
            raise CloudflareTunnelProviderError(
                f"Cloudflare API returned {response.status_code} listing tunnels: {response.text[:500]}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudflareTunnelProviderError(f"Cloudflare API returned non-JSON listing tunnels: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("success"):
            errors = payload.get("errors") if isinstance(payload, dict) else None
            raise CloudflareTunnelProviderError(f"Cloudflare API reported failure listing tunnels: {errors!r}")
        result = payload.get("result")
        if not isinstance(result, list):
            raise CloudflareTunnelProviderError(
                f"Cloudflare API returned non-list `result` for tunnels: {type(result).__name__}"
            )
        for tunnel in result:
            if not isinstance(tunnel, dict):
                continue
            metadata = tunnel.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if metadata.get("env") == expected_env:
                tunnel_id = tunnel.get("id")
                if isinstance(tunnel_id, str):
                    matches.append(tunnel_id)
        result_info = payload.get("result_info")
        total_pages = result_info.get("total_pages", 1) if isinstance(result_info, dict) else 1
        has_more_pages = page < total_pages
        page += 1
    return tuple(matches)


def delete_tunnels(
    tunnel_ids: tuple[str, ...],
    *,
    account_id: str,
    api_token: SecretStr,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Delete every Cloudflare tunnel in ``tunnel_ids`` via ``DELETE /cfd_tunnel/{id}``.

    Idempotent on a per-tunnel basis: a 404 ("already deleted") is
    treated as success. Any other failure aborts the loop so the
    operator can re-run ``minds env destroy`` after addressing the
    underlying issue (a partial cleanup leaves the env root in place
    per the deferred-removal invariant).
    """
    if not tunnel_ids:
        return
    headers = {
        "Authorization": f"Bearer {api_token.get_secret_value()}",
        "Accept": "application/json",
    }
    for tunnel_id in tunnel_ids:
        url = f"{_CF_API_BASE}/accounts/{account_id}/cfd_tunnel/{tunnel_id}"
        try:
            with httpx.Client(timeout=_REQUEST_TIMEOUT_SECONDS, transport=transport) as client:
                response = client.delete(url, headers=headers)
        except httpx.HTTPError as exc:
            raise CloudflareTunnelProviderError(f"Cloudflare API request failed (DELETE {url}): {exc}") from exc
        if response.status_code == 404:
            continue
        if response.status_code >= 400:
            raise CloudflareTunnelProviderError(
                f"Cloudflare API returned {response.status_code} deleting tunnel {tunnel_id}: {response.text[:500]}"
            )
