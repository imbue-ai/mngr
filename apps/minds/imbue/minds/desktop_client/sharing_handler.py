"""Handler for ``SharingRequestEvent`` -- the Cloudflare-tunnel sharing flow.

A sharing request asks the user to expose one of the agent's services
(e.g. ``web``) at a public Cloudflare URL with an optional email-based
ACL. Granting the request runs exactly the same Cloudflare work as the
direct ``/sharing/{agent_id}/{service_name}`` editor: create the tunnel
if needed, register the service, and apply the ACL. The two paths are
factored through :func:`enable_sharing_via_cloudflare` so they cannot
drift.

The on-the-wire response shape is intentionally a 303 redirect rather
than JSON: ``static/sharing.js`` issues the request via ``fetch`` and
then drives navigation client-side, so it does not need a body, and
form-style POSTs continue to work without JS.
"""

import json
from collections.abc import Sequence

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from loguru import logger
from pydantic import Field

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.api_v1 import get_cf_client_with_auth
from imbue.minds.desktop_client.api_v1 import inject_tunnel_token_into_agent
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import SharingRequestEvent
from imbue.minds.desktop_client.request_events import append_response_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.templates import render_sharing_editor
from imbue.minds.desktop_client.tunnel_token_store import load_tunnel_token
from imbue.minds.desktop_client.tunnel_token_store import save_tunnel_token
from imbue.minds.primitives import ServiceName
from imbue.mngr.primitives import AgentId


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


def enable_sharing_via_cloudflare(
    request: Request,
    agent_id: AgentId,
    service_name: ServiceName,
    emails: Sequence[str],
    backend_resolver: BackendResolverInterface,
) -> bool:
    """Perform the Cloudflare side-effects for enabling sharing.

    Used by both the direct sharing editor and the sharing-request grant
    flow so the two cannot drift. Returns ``True`` if the service was
    successfully registered with Cloudflare; ``False`` if any of the
    preconditions failed (no auth, no backend URL, etc.). Logs a warning
    on a soft failure but never raises -- the caller decides how to
    surface that to the user.
    """
    cf_client, _ = get_cf_client_with_auth(request, agent_id=agent_id)
    if cf_client is None:
        return False

    backend_url = backend_resolver.get_backend_url(agent_id, service_name)
    if not backend_url:
        return False

    paths: WorkspacePaths = request.app.state.api_v1_paths
    stored_token = load_tunnel_token(paths.data_dir, agent_id)
    if stored_token is None:
        token, _ = cf_client.create_tunnel(agent_id)
        if token:
            save_tunnel_token(paths.data_dir, agent_id, token)
            inject_tunnel_token_into_agent(agent_id, token)

    cf_client.add_service(agent_id, service_name, backend_url)
    if emails:
        rules: list[dict[str, object]] = [
            {"action": "allow", "include": [{"email": {"email": e}} for e in emails]},
        ]
        cf_client.set_service_auth(agent_id, str(service_name), rules)
    return True


class SharingRequestHandler(RequestEventHandler):
    """Handles the Cloudflare-sharing flow for ``SharingRequestEvent``s.

    Holds the small amount of additional state the sharing dialog needs
    (currently just the session store, used to resolve a friendly
    workspace name for the editor header). Per-request Cloudflare auth
    is still attached at call time via :func:`get_cf_client_with_auth`,
    so this object can be safely shared across requests.
    """

    session_store: MultiAccountSessionStore | None = Field(
        default=None,
        description="Used to look up the signed-in account associated with a workspace.",
    )

    # -- RequestEventHandler interface ---------------------------------------

    def handles_request_type(self) -> str:
        return str(RequestType.SHARING)

    def kind_label(self) -> str:
        return "sharing"

    def display_name_for_event(self, req_event: RequestEvent) -> str:
        if isinstance(req_event, SharingRequestEvent):
            return req_event.service_name
        return ""

    def render_request_page(
        self,
        req_event: RequestEvent,
        backend_resolver: BackendResolverInterface,
    ) -> Response:
        if not isinstance(req_event, SharingRequestEvent):
            return HTMLResponse(content="<p>Unsupported request type</p>", status_code=500)

        ws_name, account_email, has_account, accounts = self._resolve_ws_name_and_account(
            req_event.agent_id, backend_resolver
        )
        suggested = list(dict.fromkeys(req_event.suggested_emails))
        request_id = str(req_event.event_id)
        html = render_sharing_editor(
            agent_id=req_event.agent_id,
            service_name=req_event.service_name,
            title=f"Sharing Request: {req_event.service_name}",
            initial_emails=suggested,
            is_request=True,
            request_id=request_id,
            has_account=has_account,
            accounts=accounts,
            redirect_url=f"/requests/{request_id}",
            ws_name=ws_name,
            account_email=account_email,
        )
        return HTMLResponse(content=html)

    async def apply_grant_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        if not isinstance(req_event, SharingRequestEvent):
            return Response(content="Unsupported request type", status_code=500)
        backend_resolver: BackendResolverInterface = request.app.state.backend_resolver

        form = await request.form()
        emails = parse_emails_form_value(str(form.get("emails", "[]")))

        agent_id = AgentId(req_event.agent_id)
        service_name = ServiceName(req_event.service_name)
        sharing_succeeded = enable_sharing_via_cloudflare(
            request=request,
            agent_id=agent_id,
            service_name=service_name,
            emails=emails,
            backend_resolver=backend_resolver,
        )
        if not sharing_succeeded:
            # Don't write GRANTED if Cloudflare didn't accept the change:
            # the agent must not see GRANTED without the tunnel actually
            # being set up. The dialog stays open so the user can retry.
            logger.warning(
                "Sharing grant for agent {} service {} failed at Cloudflare; not writing GRANTED",
                agent_id,
                service_name,
            )
            return Response(
                status_code=303,
                headers={"Location": f"/sharing/{agent_id}/{service_name}"},
            )

        self._write_response_event_and_mirror(
            request=request,
            req_event=req_event,
            status=RequestStatus.GRANTED,
        )
        return Response(
            status_code=303,
            headers={"Location": f"/sharing/{agent_id}/{service_name}"},
        )

    async def apply_deny_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        if not isinstance(req_event, SharingRequestEvent):
            return Response(content="Unsupported request type", status_code=500)
        self._write_response_event_and_mirror(
            request=request,
            req_event=req_event,
            status=RequestStatus.DENIED,
        )
        return Response(status_code=303, headers={"Location": "/"})

    # -- Internals -----------------------------------------------------------

    def _resolve_ws_name_and_account(
        self,
        agent_id: str,
        backend_resolver: BackendResolverInterface,
    ) -> tuple[str, str, bool, list[AccountSession]]:
        """Resolve workspace name, signed-in account email, and the accounts list.

        Mirrors the layout of the inline ``_resolve_ws_name_and_account``
        helper in ``app.py`` (which still serves the direct sharing
        editor) but binds the session store at construction time so the
        handler does not have to reach into ``request.app.state`` each
        time.
        """
        parsed_id = AgentId(agent_id)
        ws_name = backend_resolver.get_workspace_name(parsed_id) or ""
        if not ws_name:
            info = backend_resolver.get_agent_display_info(parsed_id)
            ws_name = info.agent_name if info else agent_id
        account = self.session_store.get_account_for_workspace(agent_id) if self.session_store else None
        account_email = account.email if account else ""
        has_account = account is not None
        accounts: list[AccountSession] = self.session_store.list_accounts() if self.session_store else []
        return ws_name, account_email, has_account, accounts

    def _write_response_event_and_mirror(
        self,
        request: Request,
        req_event: SharingRequestEvent,
        status: RequestStatus,
    ) -> None:
        """Append a response event for ``req_event`` and mirror it into the inbox."""
        paths: WorkspacePaths = request.app.state.api_v1_paths
        response_event = create_request_response_event(
            request_event_id=str(req_event.event_id),
            status=status,
            agent_id=req_event.agent_id,
            request_type=req_event.request_type,
            service_name=req_event.service_name,
        )
        append_response_event(paths.data_dir, response_event)
        inbox: RequestInbox | None = request.app.state.request_inbox
        if inbox is not None:
            request.app.state.request_inbox = inbox.add_response(response_event)
