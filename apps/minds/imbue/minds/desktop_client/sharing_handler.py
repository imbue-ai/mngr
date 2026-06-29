"""Plugin-side helpers for the desktop-client Cloudflare-tunnel sharing flow.

Sharing is configured exclusively from the desktop client's
``/sharing/{agent_id}/{service_name}`` editor route -- agents no longer
write sharing-request events back into the inbox. This module retains
:func:`enable_sharing_via_cloudflare`, the per-account work that the
direct editor route invokes when the user enables or updates sharing
from the workspace settings UI.

All Cloudflare state is owned by the connector behind ``mngr imbue_cloud
tunnels …``; minds keeps no local tunnel-token cache. The plugin's
``create_tunnel`` is idempotent on the connector side -- calling it for
an existing tunnel returns the same token rather than rotating, so
re-injection on every grant is safe.
"""

import ipaddress
import json
from collections.abc import Sequence
from typing import Any
from typing import Final
from urllib.parse import urlparse

import httpx
from loguru import logger

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import TunnelInfo
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.tunnel_token_injection import inject_tunnel_token_into_agent
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId

_CLOUDFLARE_ACCESS_LOGIN_HOST_SUFFIX: Final[str] = "cloudflareaccess.com"
_EDGE_REDIRECT_STATUS_CODES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})

# How long the readiness probe waits on a single edge fetch before treating the
# share as not-ready-yet.
SHARE_READINESS_PROBE_TIMEOUT_SECONDS: Final[float] = 4.0


def is_share_ready_from_edge_response(status_code: int, location_header: str | None) -> bool:
    """Return True if an edge probe response shows the Cloudflare Access app is live.

    Once an Access application is published at the edge, an unauthenticated
    request to the shared hostname is redirected (302) to a
    ``*.cloudflareaccess.com`` login URL. Before publication the hostname
    returns something else (a Cloudflare error, the bare origin, a 404), so
    the presence of that Access redirect is our "ready" signal.
    """
    if status_code not in _EDGE_REDIRECT_STATUS_CODES:
        return False
    if not location_header:
        return False
    redirect_host = urlparse(location_header).hostname or ""
    return redirect_host == _CLOUDFLARE_ACCESS_LOGIN_HOST_SUFFIX or redirect_host.endswith(
        "." + _CLOUDFLARE_ACCESS_LOGIN_HOST_SUFFIX
    )


def is_probeable_share_url(url: str) -> bool:
    """Return True if ``url`` is safe for the readiness probe to fetch.

    The readiness endpoint fetches a caller-supplied URL, so we restrict it
    to absolute ``https`` URLs pointing at a public host. This keeps the
    probe from being turned into an SSRF vector against localhost or
    private-range addresses.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = parsed.hostname
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Not a literal IP -- a DNS name like the tunnel hostname; allow it.
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved)


class SharingError(RuntimeError):
    """Raised by :func:`enable_sharing_via_cloudflare` on a soft failure.

    Carries a single user-presentable message; the route handler turns it
    into a 502 + JSON body that ``static/sharing.js`` displays inline
    instead of silently navigating away.
    """


def parse_emails_form_value(form_value: str) -> list[str]:
    """Parse the ``emails`` form field (a JSON array of strings) tolerantly.

    Accepts a missing / unparseable value as "no emails", mirroring how
    the legacy ``_handle_sharing_enable`` handler behaved.
    """
    try:
        parsed = json.loads(form_value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(e) for e in parsed]


def resolve_account_email_for_workspace(
    session_store: MultiAccountSessionStore | None,
    agent_id: AgentId,
) -> str:
    """Return the email of the account that owns ``agent_id``.

    Raises :class:`SharingError` if no signed-in account is associated
    with the workspace -- without an account the plugin can't make
    authenticated calls to the connector and there's nothing useful for
    the route to do.
    """
    if session_store is None:
        raise SharingError("Session store unavailable; sign in to enable sharing.")
    account = session_store.get_account_for_workspace(str(agent_id))
    if account is None:
        raise SharingError(
            f"Workspace {agent_id} is not associated with any signed-in account; "
            "associate one from the workspace settings page first."
        )
    return str(account.email)


def enable_sharing_via_cloudflare(
    agent_id: AgentId,
    service_name: ServiceName,
    emails: Sequence[str],
    backend_resolver: BackendResolverInterface,
) -> TunnelInfo:
    """Perform the plugin-side work to enable or update sharing.

    Used by the direct sharing editor route. On success, returns the
    (idempotently created) tunnel; the caller can use ``tunnel.tunnel_name``
    for any follow-up. On any soft failure -- missing CLI, no account,
    no backend URL, plugin error -- raises :class:`SharingError` with a
    user-presentable message.
    """
    # Require at least one email: sharing with an empty Access policy would leave
    # the service publicly reachable (and the readiness probe would never go green,
    # since no Cloudflare Access redirect is ever installed). Reject up front,
    # before creating any tunnel/service side effects.
    if not emails:
        raise SharingError(
            "Sharing requires at least one email to grant access to; an empty list would expose the service publicly."
        )
    cli: ImbueCloudCli | None = get_state().imbue_cloud_cli
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured on this app.")
    session_store: MultiAccountSessionStore | None = get_state().session_store
    account_email = resolve_account_email_for_workspace(session_store, agent_id)

    backend_url = backend_resolver.get_backend_url(agent_id, service_name)
    if not backend_url:
        raise SharingError(
            f"No backend URL is registered yet for service '{service_name}' on workspace "
            f"{agent_id}; wait for the agent to publish its services and try again."
        )

    try:
        tunnel = cli.create_tunnel(account=account_email, agent_id=str(agent_id))
    except ImbueCloudCliError as exc:
        raise SharingError(f"Failed to create or fetch the tunnel: {exc}") from exc
    if tunnel.token is None:
        raise SharingError("Tunnel created but the connector did not return a Cloudflare token.")
    inject_tunnel_token_into_agent(agent_id, tunnel.token.get_secret_value())

    try:
        cli.add_service(
            account=account_email,
            tunnel_name=tunnel.tunnel_name,
            service_name=str(service_name),
            service_url=backend_url,
        )
    except ImbueCloudCliError as exc:
        raise SharingError(f"Failed to register service '{service_name}' on the tunnel: {exc}") from exc

    # ``emails`` is guaranteed non-empty (checked at the top), so the Access policy
    # is always applied -- a share is never created without one.
    try:
        cli.set_service_auth(
            account=account_email,
            tunnel_name=tunnel.tunnel_name,
            service_name=str(service_name),
            policy={"emails": list(emails)},
        )
    except ImbueCloudCliError as exc:
        raise SharingError(f"Failed to apply the access policy: {exc}") from exc
    return tunnel


def get_sharing_status(
    agent_id: AgentId,
    service_name: ServiceName,
    cli: ImbueCloudCli | None,
    session_store: MultiAccountSessionStore | None,
) -> dict[str, Any]:
    """Return the current sharing status for a service as the editor JS contract.

    Shape: ``{"enabled": bool, "url": str | None, "policy": {"emails": [...], ...}}``.
    Reads tunnel + service + per-service auth from the imbue_cloud plugin (the
    connector is the source of truth). When sharing is not yet enabled (or no CLI
    / associated account is available), reports ``enabled=False`` with a default
    policy of the workspace's associated account email.
    """
    if cli is None:
        return {"enabled": False, "url": None, "policy": {"emails": []}}
    try:
        account_email = resolve_account_email_for_workspace(session_store, agent_id)
    except SharingError as exc:
        # No associated account = no plugin call available; surface an empty
        # default rather than an error since the page already shows the
        # "associate an account" affordance for this state.
        logger.debug("Sharing status: {}", exc)
        return {"enabled": False, "url": None, "policy": {"emails": []}}

    default_policy: dict[str, Any] = {"emails": [account_email]}
    try:
        tunnel = cli.find_tunnel_for_agent(account=account_email, agent_id=str(agent_id))
    except ImbueCloudCliError as exc:
        logger.warning("Failed to list tunnels for {}: {}", agent_id, exc)
        return {"enabled": False, "url": None, "policy": default_policy}
    if tunnel is None or str(service_name) not in tunnel.services:
        return {"enabled": False, "url": None, "policy": default_policy}

    try:
        service_entries = cli.list_services(account_email, tunnel.tunnel_name)
    except ImbueCloudCliError as exc:
        logger.warning("Failed to list services for tunnel {}: {}", tunnel.tunnel_name, exc)
        service_entries = []
    hostname = next(
        (entry.get("hostname") for entry in service_entries if entry.get("service_name") == str(service_name)),
        None,
    )

    try:
        policy = cli.get_service_auth(account_email, tunnel.tunnel_name, str(service_name))
    except ImbueCloudCliError:
        try:
            policy = cli.get_tunnel_auth(account_email, tunnel.tunnel_name)
        except ImbueCloudCliError:
            policy = default_policy
    if not policy.get("emails") and not policy.get("email_domains"):
        # Empty policy means "use tunnel default"; surface the owner's email so
        # the editor doesn't render an empty ACL.
        policy = default_policy

    return {
        "enabled": True,
        "url": f"https://{hostname}" if hostname else None,
        "policy": policy,
    }


def disable_sharing(
    agent_id: AgentId,
    service_name: ServiceName,
    cli: ImbueCloudCli | None,
    session_store: MultiAccountSessionStore | None,
) -> None:
    """Disable sharing for a service by removing it from its tunnel.

    The tunnel itself is left in place so re-enabling later does not re-issue a
    fresh token. A no-op (success) when no tunnel exists yet. Raises
    :class:`SharingError` on a missing CLI, no associated account, or a plugin
    error.
    """
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured.")
    account_email = resolve_account_email_for_workspace(session_store, agent_id)
    try:
        tunnel = cli.find_tunnel_for_agent(account=account_email, agent_id=str(agent_id))
    except ImbueCloudCliError as exc:
        raise SharingError(f"Failed to look up the tunnel: {exc}") from exc
    if tunnel is None or str(service_name) not in tunnel.services:
        # Nothing to disable: either no tunnel exists yet, or the service is
        # already absent from it (e.g. a repeated disable). Idempotent success --
        # ``tunnel.services`` is the same authoritative list ``get_sharing_status``
        # reads, so this never skips a service that is actually still shared.
        return
    try:
        cli.remove_service(account=account_email, tunnel_name=tunnel.tunnel_name, service_name=str(service_name))
    except ImbueCloudCliError as exc:
        raise SharingError(f"Failed to disable sharing: {exc}") from exc


def probe_share_url_readiness(http_client: httpx.Client, url: str) -> bool:
    """Fetch ``url`` once and report whether the Cloudflare Access app is live.

    Uses the app's shared (``follow_redirects=False``) client so the Access login
    redirect is observed rather than followed. Any transport error or timeout is
    treated as "not ready yet". The caller is responsible for first validating
    ``url`` with :func:`is_probeable_share_url`.
    """
    try:
        response = http_client.get(url, timeout=SHARE_READINESS_PROBE_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.debug("Probed share URL {} but it is not ready yet: {}", url, exc)
        return False
    return is_share_ready_from_edge_response(response.status_code, response.headers.get("location"))
