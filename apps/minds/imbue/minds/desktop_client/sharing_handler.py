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

import json
from collections.abc import Sequence

from fastapi import Request

from imbue.minds.desktop_client.api_v1 import inject_tunnel_token_into_agent
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import TunnelInfo
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId


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
    request: Request,
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
    cli: ImbueCloudCli | None = request.app.state.imbue_cloud_cli
    if cli is None:
        raise SharingError("imbue_cloud CLI is not configured on this app.")
    session_store: MultiAccountSessionStore | None = request.app.state.session_store
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

    if emails:
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
