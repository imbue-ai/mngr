"""File-sharing permission flow (``RequestType.FILE_SHARING_PERMISSION``).

A file-sharing permission request asks the user to grant the agent
access to a single absolute file path on the desktop host, served
through the ``minds-api-proxy`` Latchkey extension.

Unlike :mod:`imbue.minds.desktop_client.latchkey.permissions`, the
dialog is a plain yes/no on a specific path -- there is no
per-permission editing because the request itself is already
fully-specified (one path, both read and write methods). Approval
calls ``POST /permission-requests/approve/<id>`` on the gateway's
``permission-requests`` extension; the extension owns the actual
write to the agent's ``latchkey_permissions.json`` using the
``effect`` payload it precomputed when the request was created.
Denial reuses the legacy ``DELETE /permission-requests/<id>`` path so
the gateway forgets the pending entry.
"""

import asyncio
import html as html_module
import json
from pathlib import Path
from typing import Final

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from loguru import logger
from pydantic import Field

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.permissions import MngrMessageSender
from imbue.minds.desktop_client.request_events import LatchkeyFileSharingPermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestResponseEvent
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import append_response_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.mngr.primitives import AgentId

# Label shown on the requests-panel card (lower-case, short).
_KIND_LABEL: Final[str] = "file sharing"


def _format_granted_message(file_path: str) -> str:
    return (
        f"Your file-sharing permission request for '{file_path}' was granted. Please retry the call that was blocked."
    )


def _format_denied_message(file_path: str) -> str:
    return f"Your file-sharing permission request for '{file_path}' was denied. Do not retry the blocked call."


def _json_error(message: str, status_code: int) -> Response:
    return Response(
        content=json.dumps({"error": message}),
        media_type="application/json",
        status_code=status_code,
    )


def _resolve_workspace_name(
    backend_resolver: BackendResolverInterface,
    agent_id: AgentId,
    fallback: str,
) -> str:
    ws_name = backend_resolver.get_workspace_name(agent_id) or ""
    if ws_name:
        return ws_name
    info = backend_resolver.get_agent_display_info(agent_id)
    return info.agent_name if info else fallback


def _render_dialog_page(
    request_id: str,
    agent_id: str,
    ws_name: str,
    file_path: str,
    rationale: str,
    mngr_forward_origin: str,
) -> str:
    """Render the file-sharing approval dialog.

    Plain hand-written HTML rather than a Jinja template: the page is
    small, the layout is fixed, and adding a template just for this
    one dialog isn't worth the indirection. Mirrors the visual style
    of the latchkey-permission dialog (dark background, primary button
    + secondary button) for consistency.
    """
    escaped_request_id = html_module.escape(request_id, quote=True)
    escaped_agent_id = html_module.escape(agent_id, quote=True)
    workspace_link = (
        f'<a href="{html_module.escape(mngr_forward_origin, quote=True)}/goto/{escaped_agent_id}/">'
        f"{html_module.escape(ws_name)}</a>"
        if mngr_forward_origin
        else html_module.escape(ws_name)
    )
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>File-sharing permission request</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      background: #0f172a;
      color: #cbd5e1;
      margin: 0;
      padding: 32px;
      max-width: 720px;
    }}
    h1 {{ color: #e2e8f0; font-size: 20px; margin-top: 0; }}
    .path {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      background: #1e293b;
      padding: 6px 10px;
      border-radius: 4px;
      color: #f8fafc;
      word-break: break-all;
    }}
    .rationale {{
      background: #1e293b;
      padding: 12px;
      border-radius: 6px;
      margin: 12px 0;
      white-space: pre-wrap;
    }}
    .actions {{ margin-top: 24px; display: flex; gap: 12px; }}
    button {{
      padding: 10px 20px;
      border-radius: 6px;
      font-size: 14px;
      cursor: pointer;
      border: 1px solid #334155;
    }}
    button.primary {{
      background: #1d4ed8;
      color: white;
      border-color: #1d4ed8;
    }}
    button.primary:hover {{ background: #1e40af; }}
    button.secondary {{
      background: transparent;
      color: #cbd5e1;
    }}
    button.secondary:hover {{ background: rgba(255,255,255,0.06); }}
    button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .status {{ margin-top: 16px; color: #94a3b8; font-size: 13px; }}
    .error {{ color: #f87171; }}
  </style>
</head>
<body>
  <h1>File access request</h1>
  <p>Workspace {workspace_link} is requesting access to a file on this host:</p>
  <p class="path">{html_module.escape(file_path)}</p>
  <div class="rationale">{html_module.escape(rationale)}</div>
  <p>Approving lets the agent read and write this specific file through
     the Minds API. The grant applies until you remove it from the
     workspace settings.</p>
  <div class="actions">
    <button id="approve" class="primary" onclick="submitDecision('grant')">Approve</button>
    <button id="deny" class="secondary" onclick="submitDecision('deny')">Deny</button>
  </div>
  <div id="status" class="status"></div>
  <script>
  async function submitDecision(action) {{
    const approveBtn = document.getElementById('approve');
    const denyBtn = document.getElementById('deny');
    const status = document.getElementById('status');
    approveBtn.disabled = true;
    denyBtn.disabled = true;
    status.className = 'status';
    status.textContent = action === 'grant'
      ? 'Granting access...'
      : 'Denying request...';
    try {{
      const response = await fetch('/requests/{escaped_request_id}/' + action, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: '{{}}',
      }});
      if (!response.ok) {{
        const body = await response.text();
        status.className = 'status error';
        status.textContent = 'Request failed (' + response.status + '): ' + body;
        approveBtn.disabled = false;
        denyBtn.disabled = false;
        return;
      }}
      status.textContent = action === 'grant'
        ? 'Access granted. The agent has been notified.'
        : 'Request denied. The agent has been notified.';
    }} catch (error) {{
      status.className = 'status error';
      status.textContent = 'Request failed: ' + (error && error.message);
      approveBtn.disabled = false;
      denyBtn.disabled = false;
    }}
  }}
  </script>
</body>
</html>
"""


class FileSharingGrantHandler(RequestEventHandler):
    """Per-``RequestType.FILE_SHARING_PERMISSION`` handler.

    The bulk of the work lives in the gateway's
    ``permission-requests`` extension (which owns the
    ``latchkey_permissions.json`` write). This class is therefore
    quite thin: it renders the yes/no dialog, asks the gateway to
    approve or delete the pending request via
    :class:`LatchkeyGatewayClient`, and writes the response event so
    the request stops appearing as pending.
    """

    data_dir: Path = Field(frozen=True, description="Minds data directory (typically ``~/.minds``).")
    gateway_client: LatchkeyGatewayClient = Field(
        description=(
            "HTTP client used to call ``POST /permission-requests/approve/<id>`` and "
            "``DELETE /permission-requests/<id>`` on the gateway's bundled "
            "``permission-requests`` extension."
        ),
    )
    mngr_message_sender: MngrMessageSender = Field(
        description="Sends ``mngr message`` nudges to the waiting agent on resolution.",
    )

    # -- RequestEventHandler interface ---------------------------------------

    def handles_request_type(self) -> str:
        return str(RequestType.FILE_SHARING_PERMISSION)

    def kind_label(self) -> str:
        return _KIND_LABEL

    def display_name_for_event(self, req_event: RequestEvent) -> str:
        if not isinstance(req_event, LatchkeyFileSharingPermissionRequestEvent):
            return ""
        return req_event.path

    def render_request_page(
        self,
        req_event: RequestEvent,
        backend_resolver: BackendResolverInterface,
        mngr_forward_origin: str,
    ) -> Response:
        if not isinstance(req_event, LatchkeyFileSharingPermissionRequestEvent):
            return HTMLResponse(content="<p>Unsupported request type</p>", status_code=500)
        parsed_agent_id = AgentId(req_event.agent_id)
        ws_name = _resolve_workspace_name(backend_resolver, parsed_agent_id, fallback=req_event.agent_id)
        rendered = _render_dialog_page(
            request_id=str(req_event.event_id),
            agent_id=req_event.agent_id,
            ws_name=ws_name,
            file_path=req_event.path,
            rationale=req_event.rationale,
            mngr_forward_origin=mngr_forward_origin,
        )
        return HTMLResponse(content=rendered)

    async def apply_grant_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        if not isinstance(req_event, LatchkeyFileSharingPermissionRequestEvent):
            return _json_error("Unsupported request type", status_code=500)
        request_event_id = str(req_event.event_id)
        parsed_agent_id = AgentId(req_event.agent_id)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.gateway_client.approve_permission_request(request_event_id),
            )
        except LatchkeyGatewayClientError as e:
            logger.warning(
                "Could not approve file-sharing request {} via gateway: {}",
                request_event_id,
                e,
            )
            return _json_error(
                f"Could not approve file-sharing request through the latchkey gateway: {e}",
                status_code=502,
            )

        message = _format_granted_message(req_event.path)
        response_event = self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=parsed_agent_id,
            file_path=req_event.path,
            status=RequestStatus.GRANTED,
            message=message,
        )
        self._mirror_response_into_inbox(request, response_event)
        return Response(
            content=json.dumps({"outcome": "GRANTED", "message": message}),
            media_type="application/json",
        )

    async def apply_deny_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        if not isinstance(req_event, LatchkeyFileSharingPermissionRequestEvent):
            return _json_error("Unsupported request type", status_code=500)
        request_event_id = str(req_event.event_id)
        parsed_agent_id = AgentId(req_event.agent_id)
        # DELETE tolerates 404 -- if the request is already gone we still
        # want to write the response event and notify the agent.
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self.gateway_client.delete_permission_request(request_event_id),
            )
        except LatchkeyGatewayClientError as e:
            logger.warning(
                "Could not DELETE file-sharing request {} from gateway; will rely on next-restart cleanup: {}",
                request_event_id,
                e,
            )

        message = _format_denied_message(req_event.path)
        response_event = self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=parsed_agent_id,
            file_path=req_event.path,
            status=RequestStatus.DENIED,
            message=message,
        )
        self._mirror_response_into_inbox(request, response_event)
        return Response(
            content=json.dumps({"outcome": "DENIED", "message": message}),
            media_type="application/json",
        )

    # -- Internals -----------------------------------------------------------

    def _write_response_and_notify(
        self,
        request_event_id: str,
        agent_id: AgentId,
        file_path: str,
        status: RequestStatus,
        message: str,
    ) -> RequestResponseEvent:
        """Persist the response event and ping the agent. Returns the new event."""
        response_event = create_request_response_event(
            request_event_id=request_event_id,
            status=status,
            agent_id=str(agent_id),
            request_type=str(RequestType.FILE_SHARING_PERMISSION),
            # File-sharing requests are deduplicated by ``path`` (see
            # ``_dedup_key`` in ``request_events.py``); reuse that
            # field so the response collapses with the matching
            # request entry rather than leaking past it.
            scope=file_path,
        )
        append_response_event(self.data_dir, response_event)
        self.mngr_message_sender.send(agent_id, message)
        return response_event

    def _mirror_response_into_inbox(
        self,
        request: Request,
        response_event: RequestResponseEvent,
    ) -> None:
        """Mirror the on-disk response event into the in-memory inbox.

        Without this the resolved card stays visible in the requests
        panel until the next desktop-client restart. Also wakes the
        chrome SSE so the new ``request_count`` is pushed without
        waiting for the 30s heartbeat.
        """
        inbox: RequestInbox | None = request.app.state.request_inbox
        if inbox is None:
            return
        request.app.state.request_inbox = inbox.add_response(response_event)
        backend_resolver: BackendResolverInterface = request.app.state.backend_resolver
        if isinstance(backend_resolver, MngrCliBackendResolver):
            backend_resolver.notify_change()
