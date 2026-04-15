"""Proxy helpers for communicating with the minds desktop client REST API.

The minds desktop client exposes its API to agents via a reverse SSH tunnel.
The URL is written to ``$MNGR_AGENT_STATE_DIR/minds_api_url``. Authentication
uses the ``MINDS_API_KEY`` environment variable as a Bearer token.

Only read-only status queries are proxied through the desktop client API.
Mutation operations (enable, disable, update auth) are handled via request
events written to ``requests/events.jsonl``.
"""

import os
from pathlib import Path
from typing import Final

import httpx
from loguru import logger as _loguru_logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds_workspace_server.request_writer import write_sharing_request

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


def request_sharing_edit(server_name: str, is_user_requested: bool = True) -> None:
    """Create a sharing request event for editing sharing settings.

    Reads the current status via the desktop client API, then writes a
    request event to ``requests/events.jsonl`` with the current state
    as structured data for pre-populating the desktop client form.
    """
    agent_id = _get_own_agent_id()

    # Try to get current status for pre-population
    current_status_dict: dict[str, object] | None = None
    suggested_emails: list[str] = []
    try:
        status = get_sharing_status(server_name)
        current_status_dict = {
            "enabled": status.enabled,
            "url": status.url,
            "auth_rules": status.auth_rules,
        }
        # Extract emails from auth rules for convenience
        for rule in status.auth_rules:
            includes = rule.get("include", [])
            if not isinstance(includes, list):
                continue
            for inc in includes:
                if isinstance(inc, dict):
                    email_obj = inc.get("email")
                    if isinstance(email_obj, dict):
                        email = email_obj.get("email")
                        if email:
                            suggested_emails.append(str(email))
    except SharingProxyError:
        logger.debug("Could not fetch current sharing status for pre-population")

    write_sharing_request(
        agent_id=agent_id,
        server_name=server_name,
        is_user_requested=is_user_requested,
        current_status=current_status_dict,
        suggested_emails=suggested_emails,
    )


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
