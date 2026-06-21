"""File-sharing permission grant/deny flow (``RequestType.FILE_SHARING_PERMISSION``).

This module is one of the two sibling handlers under
:mod:`imbue.minds.desktop_client.latchkey.handlers`. It owns the
flow for *file-sharing* permission requests: rendering the yes/no
dialog for a single absolute file path, calling the gateway's
``permission-requests`` extension to approve or drop the request,
appending the response event, and notifying the waiting agent via
``mngr message``.

A file-sharing permission request asks the user to grant the agent
access to a single absolute file path on the desktop host, served
through the ``minds-api-proxy`` Latchkey extension. Unlike its
:mod:`.predefined` sibling, there is no per-permission checkbox list:
the request already names the single (path, access) pair. The dialog
does, however, let the user *edit the shared path* before approving
(the agent-requested path is pre-filled into an editable field, and a
native file picker in the desktop app can fill it in). The access mode
is fixed at request-creation time and is not user-editable.

Approval calls ``POST /permission-requests/approve/<id>`` on the
gateway's ``permission-requests`` extension; the extension owns the
actual write to the agent's ``latchkey_permissions.json``. When the
user left the path unchanged the extension uses the ``effect`` payload
it precomputed at request-creation time; when the user edited the path
we send it as a ``{"path": ...}`` body and the extension recomputes the
file-sharing effect for that path (re-validating it for traversal).
Denial reuses the legacy ``DELETE /permission-requests/<id>`` path so
the gateway forgets the pending entry.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from flask import Request
from flask import Response
from loguru import logger
from pydantic import Field

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey.gateway_client import FileSharingAccess
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.templates import render_file_sharing_permission_dialog
from imbue.minds.desktop_client.request_events import LatchkeyFileSharingPermissionRequestEvent
from imbue.minds.desktop_client.request_events import RequestEvent
from imbue.minds.desktop_client.request_events import RequestInbox
from imbue.minds.desktop_client.request_events import RequestResponseEvent
from imbue.minds.desktop_client.request_events import RequestStatus
from imbue.minds.desktop_client.request_events import RequestType
from imbue.minds.desktop_client.request_events import append_response_event
from imbue.minds.desktop_client.request_events import create_request_response_event
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.webdav import get_file_sharing_roots
from imbue.mngr.primitives import AgentId

# Label shown on the inbox list card (lower-case, short).
_KIND_LABEL: Final[str] = "file sharing"

# Form field carrying the (possibly user-edited) absolute path to share.
# The dialog pre-fills it with the agent-requested path; the user may
# paste a different one or pick it from a native file dialog.
_FILE_PATH_FIELD: Final[str] = "file_path"


class InvalidSharePathError(Exception):
    """Raised when a user-edited share path is not an acceptable absolute path."""


def _is_path_within_roots(path: str, allowed_roots: Sequence[Path]) -> bool:
    """Whether ``path`` is at or beneath one of ``allowed_roots``.

    Case-insensitive and purely lexical, mirroring how the WebDAV server
    matches its mount prefixes (WsgiDAV lowercases both the share keys
    and the request path) so we never reject a path the server would
    actually serve.
    """
    lower_path = path.lower()
    for root in allowed_roots:
        lower_root = str(root).rstrip("/").lower() or "/"
        if lower_path == lower_root or lower_path.startswith(f"{lower_root}/"):
            return True
    return False


def _expand_home_prefix(path: str, home_dir: Path) -> str:
    """Expand a leading ``~`` / ``~/`` to ``home_dir``.

    Mirrors the gateway's ``expandFileSharingHomePrefix``: a bare ``~``
    or a ``~/...`` prefix expands against the user's home directory (the
    home WebDAV mount root); ``~user`` notation for another user's home
    cannot be resolved here and is rejected. The expansion is a pure
    string splice (not ``Path`` joining) so any ``..`` in the remainder
    survives into the result and is still caught by the traversal check
    in ``_normalize_share_path``.
    """
    if path == "~" or path.startswith("~/"):
        return f"{home_dir}{path[1:]}"
    if path.startswith("~"):
        raise InvalidSharePathError(
            "The path to share uses unsupported '~user' notation; only '~' or '~/...' "
            f"(your home directory) is accepted: {path}"
        )
    return path


def _normalize_share_path(raw_path: str, allowed_roots: Sequence[Path], home_dir: Path) -> str:
    """Validate and normalize a user-edited share path.

    Mirrors the gateway's ``validateAbsoluteFileSharingPath`` so the user
    gets a clear, immediate error instead of a generic gateway 4xx. The
    gateway re-validates on approve regardless -- this is a friendlier
    first line of defence, not the security boundary.

    Expands a leading ``~`` / ``~/`` to ``home_dir``, then rejects empty,
    relative, and ``..``-containing paths, and paths that fall outside
    ``allowed_roots`` (the WebDAV mount roots: home + temp). Returns the
    expanded, stripped path on success.
    """
    path = _expand_home_prefix(raw_path.strip(), home_dir)
    if not path:
        raise InvalidSharePathError("The path to share must not be empty.")
    if not path.startswith("/"):
        raise InvalidSharePathError(
            f"The path to share must be absolute (start with '/') or use '~' / '~/...': {path}"
        )
    # Reject any ``..`` segment regardless of separator, matching the
    # gateway's traversal check.
    if any(segment == ".." for segment in path.replace("\\", "/").split("/")):
        raise InvalidSharePathError(f"The path to share must not contain a '..' segment: {path}")
    if not _is_path_within_roots(path, allowed_roots):
        roots_str = ", ".join(str(root) for root in allowed_roots)
        raise InvalidSharePathError(f"The path to share must be within a shared folder ({roots_str}): {path}")
    return path


def _access_human_label(access: str) -> str:
    """Lower-case human phrase for the access mode ("read-only" / "read & write")."""
    if access == str(FileSharingAccess.READ):
        return "read-only"
    if access == str(FileSharingAccess.WRITE):
        return "read & write"
    # Unknown access values are unexpected but possible if the gateway
    # ever grows a new mode -- surface the raw value rather than
    # crashing so the dialog still renders.
    return access


def _format_granted_message(file_path: str, access: str) -> str:
    return f"Your {_access_human_label(access)} file-sharing permission request for '{file_path}' was granted."


def _format_denied_message(file_path: str, access: str) -> str:
    return f"Your {_access_human_label(access)} file-sharing permission request for '{file_path}' was denied."


def _json_error(message: str, status_code: int) -> Response:
    return make_response(
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
    share_roots: tuple[Path, ...] = Field(
        default_factory=get_file_sharing_roots,
        frozen=True,
        description=(
            "On-disk roots the WebDAV file server mounts (home + temp). A requested or "
            "user-edited path outside these is rejected up front with a clear error rather "
            "than being forwarded to the gateway. Defaults to the live WebDAV mount roots."
        ),
    )
    home_dir: Path = Field(
        default_factory=Path.home,
        frozen=True,
        description=(
            "The user's home directory, used to expand a leading ``~`` / ``~/`` in a "
            "user-edited share path (mirroring the gateway). Defaults to ``Path.home()``, "
            "the home WebDAV mount root."
        ),
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

    def render_request_detail_fragment(
        self,
        req_event: RequestEvent,
        backend_resolver: BackendResolverInterface,
        mngr_forward_origin: str,
    ) -> str:
        if not isinstance(req_event, LatchkeyFileSharingPermissionRequestEvent):
            return "<p>Unsupported request type</p>"
        parsed_agent_id = AgentId(req_event.agent_id)
        ws_name = _resolve_workspace_name(backend_resolver, parsed_agent_id, fallback=req_event.agent_id)
        return render_file_sharing_permission_dialog(
            agent_id=req_event.agent_id,
            request_id=str(req_event.event_id),
            ws_name=ws_name,
            rationale=req_event.rationale,
            file_path=req_event.path,
            access=req_event.access,
            access_human_label=_access_human_label(req_event.access),
            allowed_roots_json=json.dumps([str(root) for root in self.share_roots]),
            home_dir=str(self.home_dir),
            mngr_forward_origin=mngr_forward_origin,
        )

    def apply_grant_request(
        self,
        request: Request,
        req_event: RequestEvent,
    ) -> Response:
        if not isinstance(req_event, LatchkeyFileSharingPermissionRequestEvent):
            return _json_error("Unsupported request type", status_code=500)
        request_event_id = str(req_event.event_id)
        parsed_agent_id = AgentId(req_event.agent_id)

        # The dialog lets the user edit the shared path (paste or native
        # file picker) before approving. Read the submitted value, falling
        # back to the agent-requested path when the field is absent (e.g.
        # an older client). Validate it up front for a friendly error; the
        # gateway re-validates on approve.
        form = request.form
        raw_override = form.get(_FILE_PATH_FIELD)
        try:
            effective_path = (
                _normalize_share_path(str(raw_override), self.share_roots, self.home_dir)
                if raw_override is not None
                else req_event.path
            )
        except InvalidSharePathError as e:
            return _json_error(str(e), status_code=400)

        # Only send an override to the gateway when the user actually
        # changed the path; otherwise the gateway applies the precomputed
        # effect verbatim (and we avoid recomputation for the common case).
        override_path = effective_path if effective_path != req_event.path else None
        try:
            self.gateway_client.approve_permission_request(
                request_event_id,
                override_path=override_path,
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

        message = _format_granted_message(effective_path, req_event.access)
        response_event = self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=parsed_agent_id,
            file_path=effective_path,
            status=RequestStatus.GRANTED,
            message=message,
        )
        self._mirror_response_into_inbox(response_event)
        return make_response(
            content=json.dumps({"outcome": "GRANTED", "message": message}),
            media_type="application/json",
        )

    def apply_deny_request(
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
            self.gateway_client.delete_permission_request(request_event_id)
        except LatchkeyGatewayClientError as e:
            logger.warning(
                "Could not DELETE file-sharing request {} from gateway; will rely on next-restart cleanup: {}",
                request_event_id,
                e,
            )

        message = _format_denied_message(req_event.path, req_event.access)
        response_event = self._write_response_and_notify(
            request_event_id=request_event_id,
            agent_id=parsed_agent_id,
            file_path=req_event.path,
            status=RequestStatus.DENIED,
            message=message,
        )
        self._mirror_response_into_inbox(response_event)
        return make_response(
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
            # ``scope`` on a response is informational only -- the
            # inbox joins responses to requests on
            # ``request_event_id`` (see ``get_pending_requests`` in
            # ``request_events.py``). We still record the file path
            # here so the persisted response carries the resolved
            # path for debugging.
            scope=file_path,
        )
        append_response_event(self.data_dir, response_event)
        self.mngr_message_sender.send(agent_id, message)
        return response_event

    def _mirror_response_into_inbox(
        self,
        response_event: RequestResponseEvent,
    ) -> None:
        """Mirror the on-disk response event into the in-memory inbox.

        Without this the resolved card stays visible in the requests
        panel until the next desktop-client restart. Also wakes the
        chrome SSE so the new ``requests`` payload is pushed without
        waiting for the 30s heartbeat.
        """
        inbox: RequestInbox | None = get_state().request_inbox
        if inbox is None:
            return
        get_state().request_inbox = inbox.add_response(response_event)
        backend_resolver: BackendResolverInterface = get_state().backend_resolver
        if isinstance(backend_resolver, MngrCliBackendResolver):
            backend_resolver.notify_change()
