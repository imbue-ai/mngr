"""Proxy helpers for communicating with the minds desktop client REST API.

The minds desktop client exposes its API to agents via a reverse SSH tunnel.
The URL is written to ``$MNGR_AGENT_STATE_DIR/minds_api_url``. Authentication
uses the ``MINDS_API_KEY`` environment variable as a Bearer token.

All sharing operations (GET status, PUT enable, DELETE disable) are proxied
through the desktop client API.
"""

import os
from pathlib import Path
from typing import Final

import httpx
from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

logger = _loguru_logger

_MINDS_API_URL_FILENAME: Final[str] = "minds_api_url"
_REQUEST_TIMEOUT_SECONDS: Final[float] = 60.0


class SharingProxyError(RuntimeError):
    """Raised when the sharing proxy cannot communicate with the desktop client."""

    ...


class SharingStatus(FrozenModel):
    """Forwarding status for a server."""

    enabled: bool = Field(description="Whether Cloudflare forwarding is active for this server")
    url: str | None = Field(default=None, description="The global URL if forwarding is enabled")
    auth_rules: list[dict[str, object]] = Field(
        default_factory=list,
        description="Cloudflare Access auth policy rules (who has access)",
    )


def _read_minds_api_url() -> str:
    """Read the minds desktop client API URL from the agent state directory.

    Raises SharingProxyError if the file is missing or unreadable.
    """
    agent_state_dir = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not agent_state_dir:
        raise SharingProxyError("MNGR_AGENT_STATE_DIR environment variable is not set")

    url_file = Path(agent_state_dir) / _MINDS_API_URL_FILENAME
    if not url_file.exists():
        raise SharingProxyError(f"Minds API URL file not found: {url_file}")

    url = url_file.read_text().strip()
    if not url:
        raise SharingProxyError(f"Minds API URL file is empty: {url_file}")

    return url


def _get_desktop_client_auth_headers() -> dict[str, str]:
    """Build authorization headers for the desktop client using MINDS_API_KEY."""
    api_key = os.environ.get("MINDS_API_KEY", "")
    if not api_key:
        raise SharingProxyError("MINDS_API_KEY environment variable is not set")
    return {"Authorization": f"Bearer {api_key}"}


def _get_own_agent_id() -> str:
    """Return this server's own agent ID from the environment."""
    agent_id = os.environ.get("MNGR_AGENT_ID", "")
    if not agent_id:
        raise SharingProxyError("MNGR_AGENT_ID environment variable is not set")
    return agent_id


def _cloudflare_url(server_name: str) -> str:
    """Build the desktop client API URL for a server's Cloudflare forwarding."""
    base_url = _read_minds_api_url()
    agent_id = _get_own_agent_id()
    return f"{base_url}/api/v1/agents/{agent_id}/servers/{server_name}/cloudflare"


def get_sharing_status(server_name: str) -> SharingStatus:
    """Fetch the current Cloudflare forwarding status for a server.

    Queries the desktop client's GET cloudflare endpoint, which returns
    ``{"enabled": bool, "url": str | null}``.
    """
    url = _cloudflare_url(server_name)
    headers = _get_desktop_client_auth_headers()

    try:
        response = httpx.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)

        if response.status_code == 200:
            data = response.json()
            return SharingStatus(
                enabled=data.get("enabled", False),
                url=data.get("url"),
                auth_rules=data.get("auth_rules", []),
            )

        error_msg = _extract_error(response)
        raise SharingProxyError(f"Failed to query sharing status: {error_msg}")

    except httpx.HTTPError as e:
        raise SharingProxyError(f"Failed to communicate with desktop client: {e}") from e


def enable_sharing(server_name: str, auth_rules: list[dict[str, object]] | None = None) -> SharingStatus:
    """Enable Cloudflare forwarding for a server via the desktop client API.

    After enabling, queries the status to get the resulting URL.
    """
    url = _cloudflare_url(server_name)
    headers = _get_desktop_client_auth_headers()

    body: dict[str, object] = {}
    if auth_rules is not None:
        body["auth_rules"] = auth_rules

    try:
        response = httpx.put(url, headers=headers, json=body if body else None, timeout=_REQUEST_TIMEOUT_SECONDS)
        if response.status_code == 200:
            return get_sharing_status(server_name)

        error_msg = _extract_error(response)
        raise SharingProxyError(f"Failed to enable sharing: {error_msg}")

    except httpx.HTTPError as e:
        raise SharingProxyError(f"Failed to communicate with desktop client: {e}") from e


def update_sharing_auth(server_name: str, auth_rules: list[dict[str, object]]) -> SharingStatus:
    """Update the auth policy for an already-enabled service."""
    url = _cloudflare_url(server_name)
    headers = _get_desktop_client_auth_headers()

    try:
        response = httpx.put(
            url,
            headers=headers,
            json={"auth_rules": auth_rules},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            return get_sharing_status(server_name)

        error_msg = _extract_error(response)
        raise SharingProxyError(f"Failed to update sharing auth: {error_msg}")

    except httpx.HTTPError as e:
        raise SharingProxyError(f"Failed to communicate with desktop client: {e}") from e


def disable_sharing(server_name: str) -> SharingStatus:
    """Disable Cloudflare forwarding for a server via the desktop client API."""
    url = _cloudflare_url(server_name)
    headers = _get_desktop_client_auth_headers()

    try:
        response = httpx.delete(url, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
        if response.status_code == 200:
            return SharingStatus(enabled=False)

        error_msg = _extract_error(response)
        raise SharingProxyError(f"Failed to disable sharing: {error_msg}")

    except httpx.HTTPError as e:
        raise SharingProxyError(f"Failed to communicate with desktop client: {e}") from e


def _extract_error(response: httpx.Response) -> str:
    """Extract an error message from a non-200 response."""
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            data = response.json()
            return str(data.get("error", f"HTTP {response.status_code}"))
        except (ValueError, KeyError):
            pass
    return f"HTTP {response.status_code}"
