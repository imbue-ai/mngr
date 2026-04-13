"""REST API v1 router for the minds desktop client.

Provides authenticated JSON endpoints for Cloudflare forwarding,
Telegram bot setup, and user notifications. Authentication uses
per-agent API keys (Bearer tokens) with SHA-256 hash lookup.
"""

import json
import os
import shlex
from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import Response
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.config.data_types import MNGR_BINARY
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.api_key_store import find_agent_by_api_key
from imbue.minds.desktop_client.cloudflare_client import CloudflareForwardingClient
from imbue.minds.desktop_client.cloudflare_client import CloudflareForwardingUrl
from imbue.minds.desktop_client.deps import BackendResolverDep
from imbue.minds.desktop_client.supertokens_auth import SuperTokensSessionStore
from imbue.minds.desktop_client.tunnel_token_store import load_tunnel_token
from imbue.minds.desktop_client.tunnel_token_store import save_tunnel_token
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.notification import NotificationRequest
from imbue.minds.desktop_client.notification import NotificationUrgency
from imbue.minds.primitives import OutputFormat
from imbue.minds.primitives import ServerName
from imbue.minds.utils.output import emit_event
from imbue.minds.telegram.credential_store import load_agent_bot_credentials
from imbue.minds.telegram.setup import TelegramSetupOrchestrator
from imbue.minds.telegram.setup import TelegramSetupStatus
from imbue.mngr.primitives import AgentId


def _authenticate_api_key(request: Request) -> AgentId:
    """Extract and validate the Bearer token from the Authorization header.

    Returns the AgentId of the caller. Raises HTTPException with 401 if the
    token is missing, malformed, or does not match any stored API key hash.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    token = auth_header[len("Bearer "):]
    if not token:
        raise HTTPException(status_code=401, detail="Empty Bearer token")

    paths: WorkspacePaths = request.app.state.api_v1_paths
    agent_id = find_agent_by_api_key(paths.data_dir, token)
    if agent_id is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return agent_id


CallerAgentIdDep = Annotated[AgentId, Depends(_authenticate_api_key)]


def inject_tunnel_token_into_agent(agent_id: AgentId, token: str) -> None:
    """Write the tunnel token to the agent's runtime/secrets via mngr exec.

    This causes the cloudflare-tunnel service inside the agent to detect
    the token and start cloudflared.
    """
    safe_token = shlex.quote(token)
    cg = ConcurrencyGroup(name="inject-tunnel-token")
    with cg:
        result = cg.run_process_to_completion(
            command=[
                MNGR_BINARY,
                "exec",
                str(agent_id),
                f"mkdir -p runtime && printf 'export CLOUDFLARE_TUNNEL_TOKEN=%s\\n' {safe_token} > runtime/secrets",
            ],
            is_checked_after=False,
        )
    if result.returncode != 0:
        logger.warning("Failed to inject tunnel token into agent {}: {}", agent_id, result.stderr.strip())


def _json_response(data: dict[str, object], status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


def _json_error(message: str, status_code: int) -> Response:
    return _json_response({"error": message}, status_code=status_code)


def _get_cf_client_with_auth(request: Request) -> tuple[CloudflareForwardingClient | None, Response | None]:
    """Get a cloudflare client enriched with SuperTokens auth if available.

    Returns (client, None) on success, or (None, error_response) if auth is required but missing.
    When SuperTokens is configured and the user is signed in, the client uses Bearer auth.
    Otherwise falls back to the original Basic Auth client.
    """
    cf_client: CloudflareForwardingClient | None = request.app.state.cloudflare_client
    session_store: SuperTokensSessionStore | None = request.app.state.supertokens_session_store

    if cf_client is None and session_store is None:
        return None, _json_error("Cloudflare forwarding not configured", 501)

    # If SuperTokens is configured, require a valid session
    if session_store is not None:
        user_info = session_store.get_user_info()
        access_token = session_store.get_access_token()
        if user_info is None or access_token is None:
            output_format: OutputFormat = request.app.state.supertokens_output_format
            emit_event("auth_required", {"message": "Sign in required for sharing"}, output_format)
            return None, _json_response(
                {"error": "Not signed in to Imbue", "auth_required": True},
                status_code=401,
            )

        # Build a client with SuperTokens Bearer auth
        forwarding_url = cf_client.forwarding_url if cf_client else None
        if forwarding_url is None:
            forwarding_url_str = os.environ.get("CLOUDFLARE_FORWARDING_URL", "")
            if not forwarding_url_str:
                return None, _json_error("Cloudflare forwarding URL not configured", 501)
            forwarding_url = CloudflareForwardingUrl(forwarding_url_str)

        enriched_client = CloudflareForwardingClient(
            forwarding_url=forwarding_url,
            username=cf_client.username if cf_client else None,
            secret=cf_client.secret if cf_client else None,
            owner_email=cf_client.owner_email if cf_client else None,
            supertokens_token=access_token,
            supertokens_user_id_prefix=str(user_info.user_id_prefix),
            supertokens_email=user_info.email,
        )
        return enriched_client, None

    # No SuperTokens, use the original client (Basic Auth)
    if cf_client is None:
        return None, _json_error("Cloudflare forwarding not configured", 501)
    return cf_client, None


# -- Request body models --


class _CloudflareEnableBody(FrozenModel):
    """Optional request body for the Cloudflare enable endpoint."""

    service_url: str | None = Field(
        default=None,
        description="Service URL to register. If omitted, resolved from the backend resolver.",
    )
    auth_rules: list[dict[str, object]] | None = Field(
        default=None,
        description="Auth policy rules to apply. If omitted, uses tunnel default.",
    )


# -- Cloudflare forwarding routes --


def _handle_cloudflare_status(
    agent_id: str,
    server_name: str,
    request: Request,
    _caller_agent_id: CallerAgentIdDep,
) -> Response:
    """Get Cloudflare forwarding status for a server."""
    cf_client, error_response = _get_cf_client_with_auth(request)
    if error_response is not None:
        return error_response
    assert cf_client is not None

    parsed_id = AgentId(agent_id)

    # Build the default auth rules from the owner email for when no policy is stored
    effective_email = cf_client._effective_owner_email()
    owner_default_rules = (
        [{"action": "allow", "include": [{"email": {"email": effective_email}}]}]
        if effective_email
        else []
    )

    services = cf_client.list_services(parsed_id)
    if services is None:
        # No tunnel exists yet -- return owner email as the default
        default_rules = cf_client.get_tunnel_auth(parsed_id)
        return _json_response({"enabled": False, "url": None, "auth_rules": default_rules or owner_default_rules})

    hostname = services.get(server_name)
    if hostname:
        # Service is enabled -- get its specific auth policy
        auth_rules = cf_client.get_service_auth(parsed_id, server_name)
        if auth_rules is None:
            auth_rules = cf_client.get_tunnel_auth(parsed_id) or owner_default_rules
        return _json_response({"enabled": True, "url": f"https://{hostname}", "auth_rules": auth_rules})

    # Tunnel exists but this service isn't enabled -- return tunnel default or owner email
    default_rules = cf_client.get_tunnel_auth(parsed_id)
    return _json_response({"enabled": False, "url": None, "auth_rules": default_rules or owner_default_rules})


def _handle_cloudflare_enable(
    agent_id: str,
    server_name: str,
    request: Request,
    _caller_agent_id: CallerAgentIdDep,
    backend_resolver: BackendResolverDep,
    body: _CloudflareEnableBody | None = None,
) -> Response:
    """Enable Cloudflare forwarding for a server."""
    cf_client, error_response = _get_cf_client_with_auth(request)
    if error_response is not None:
        return error_response
    assert cf_client is not None

    parsed_id = AgentId(agent_id)
    parsed_server = ServerName(server_name)

    service_url = body.service_url if body is not None else None
    if service_url is None:
        backend_url = backend_resolver.get_backend_url(parsed_id, parsed_server)
        if backend_url is None:
            return _json_error("Server not found locally", 404)
        service_url = backend_url

    # Ensure the tunnel exists and we have a token for it.
    # create_tunnel is idempotent -- if the tunnel already exists, it returns
    # the existing token. We always need the token to inject into the agent.
    paths: WorkspacePaths = request.app.state.api_v1_paths
    stored_token = load_tunnel_token(paths.data_dir, parsed_id)
    if stored_token is None:
        token, message = cf_client.create_tunnel(parsed_id)
        if token is None:
            return _json_error(f"Failed to create Cloudflare tunnel: {message}", 502)
        save_tunnel_token(paths.data_dir, parsed_id, token)
        inject_tunnel_token_into_agent(parsed_id, token)

    is_success = cf_client.add_service(parsed_id, parsed_server, service_url)
    if not is_success:
        return _json_error("Cloudflare API call failed", 502)

    # Apply auth rules if provided
    auth_rules = body.auth_rules if body is not None else None
    if auth_rules is not None:
        cf_client.set_service_auth(parsed_id, str(parsed_server), auth_rules)

    return _json_response({"ok": True})


def _handle_cloudflare_disable(
    agent_id: str,
    server_name: str,
    request: Request,
    _caller_agent_id: CallerAgentIdDep,
) -> Response:
    """Disable Cloudflare forwarding for a server."""
    cf_client, error_response = _get_cf_client_with_auth(request)
    if error_response is not None:
        return error_response
    assert cf_client is not None

    parsed_id = AgentId(agent_id)
    parsed_server = ServerName(server_name)

    is_success = cf_client.remove_service(parsed_id, parsed_server)

    if is_success:
        return _json_response({"ok": True})
    return _json_error("Cloudflare API call failed", 502)


# -- Telegram routes --


async def _handle_telegram_setup(
    agent_id: str,
    request: Request,
    _caller_agent_id: CallerAgentIdDep,
) -> Response:
    """Start Telegram bot setup for an agent."""
    telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
    if telegram_orchestrator is None:
        return _json_error("Telegram setup not configured", 501)

    parsed_id = AgentId(agent_id)

    agent_name = str(parsed_id)[:8]
    try:
        body = await request.json()
        raw_name = body.get("agent_name", agent_name)
        agent_name = str(raw_name).strip() if raw_name else agent_name
    except (json.JSONDecodeError, ValueError, AttributeError):
        pass

    telegram_orchestrator.start_setup(agent_id=parsed_id, agent_name=agent_name)
    return _json_response({
        "agent_id": str(parsed_id),
        "status": str(TelegramSetupStatus.CHECKING_CREDENTIALS),
    })


def _handle_telegram_status(
    agent_id: str,
    _caller_agent_id: CallerAgentIdDep,
    request: Request,
) -> Response:
    """Get Telegram setup status for an agent."""
    telegram_orchestrator: TelegramSetupOrchestrator | None = request.app.state.telegram_orchestrator
    if telegram_orchestrator is None:
        return _json_error("Telegram setup not configured", 501)

    parsed_id = AgentId(agent_id)
    info = telegram_orchestrator.get_setup_info(parsed_id)

    if info is None:
        is_active = telegram_orchestrator.agent_has_telegram(parsed_id)
        if is_active:
            paths: WorkspacePaths = request.app.state.api_v1_paths
            credentials = load_agent_bot_credentials(paths.data_dir, parsed_id)
            result: dict[str, object] = {
                "agent_id": str(parsed_id),
                "status": str(TelegramSetupStatus.DONE),
            }
            if credentials is not None and credentials.bot_username is not None:
                result["bot_username"] = credentials.bot_username
            return _json_response(result)
        return _json_error("No Telegram setup in progress for this agent", 404)

    result: dict[str, object] = {
        "agent_id": str(info.agent_id),
        "status": str(info.status),
    }
    if info.error is not None:
        result["error"] = info.error
    if info.bot_username is not None:
        result["bot_username"] = info.bot_username
    return _json_response(result)


# -- Notification route --


async def _handle_notification(
    request: Request,
    caller_agent_id: CallerAgentIdDep,
    backend_resolver: BackendResolverDep,
) -> Response:
    """Send a notification to the user."""
    dispatcher: NotificationDispatcher | None = request.app.state.notification_dispatcher
    if dispatcher is None:
        return _json_error("Notification dispatch not configured", 501)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _json_error("Invalid JSON body", 400)

    if not isinstance(body, dict):
        return _json_error("Request body must be a JSON object", 400)

    message = body.get("message")
    if not message or not isinstance(message, str):
        return _json_error("'message' field is required and must be a string", 400)

    title = body.get("title")
    if title is not None and not isinstance(title, str):
        return _json_error("'title' field must be a string", 400)
    urgency_str = body.get("urgency", "NORMAL")
    try:
        urgency = NotificationUrgency(urgency_str.upper())
    except (ValueError, AttributeError):
        return _json_error(
            f"Invalid urgency: {urgency_str}. Must be one of: low, normal, critical", 400
        )

    notification_request = NotificationRequest(
        message=message,
        title=title,
        urgency=urgency,
    )

    # Resolve calling agent's display name
    agent_info = backend_resolver.get_agent_display_info(caller_agent_id)
    agent_display_name = agent_info.agent_name if agent_info else str(caller_agent_id)

    dispatcher.dispatch(notification_request, agent_display_name)
    return _json_response({"ok": True})


# -- Router factory --


def create_api_v1_router() -> APIRouter:
    """Create the /api/v1/ router with all REST API endpoints."""
    router = APIRouter()

    # Cloudflare forwarding
    router.get(
        "/agents/{agent_id}/servers/{server_name}/cloudflare",
    )(_handle_cloudflare_status)
    router.put(
        "/agents/{agent_id}/servers/{server_name}/cloudflare",
    )(_handle_cloudflare_enable)
    router.delete(
        "/agents/{agent_id}/servers/{server_name}/cloudflare",
    )(_handle_cloudflare_disable)

    # Telegram
    router.post(
        "/agents/{agent_id}/telegram",
    )(_handle_telegram_setup)
    router.get(
        "/agents/{agent_id}/telegram",
    )(_handle_telegram_status)

    # Notifications
    router.post(
        "/notifications",
    )(_handle_notification)

    return router
