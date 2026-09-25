"""File-sharing permission grant/deny flow (wire ``request_type == "file-sharing"``).

This module is one of the two sibling handlers under
:mod:`imbue.minds.desktop_client.latchkey.handlers`. It owns the
flow for *file-sharing* permission requests: rendering the yes/no
dialog for a single absolute file path, calling the gateway's
``permission-requests`` extension to approve or drop the request,
appending the response event, and nudging the request's chat with the
verdict (:mod:`.messaging`).

A file-sharing permission request asks the user to grant the agent
access to a single absolute file path on the desktop host, served
through the ``minds-api-proxy`` Latchkey extension. Unlike its
:mod:`.predefined` sibling, there is no per-permission checkbox list:
the request already names the single (path, access) pair. The dialog
does, however, let the user *edit the shared path* before approving
(the agent-requested path is pre-filled into an editable field, and a
native file picker in the desktop app can fill it in). The access mode
is fixed at request-creation time and is not user-editable.

The request may also ask for a *synchronized copy* of the folder on the
workspace's machine. That is not a permission -- the gateway carries the
ask and never acts on it -- so the dialog offers it as the same checkbox
the Local files pane draws, ticked the way the agent asked, and this
handler starts the sync once the grant has landed. A sync that cannot be
started is refused before the grant, so the request stays pending with
the reason and the user can untick it or pick another folder.

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
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from flask import Request
from flask import Response
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import resolve_workspace_display_name
from imbue.minds.desktop_client.folder_sync import FolderSyncManager
from imbue.minds.desktop_client.folder_sync_settings import FolderSyncConflict
from imbue.minds.desktop_client.latchkey.gateway_client import FileSharingAccess
from imbue.minds.desktop_client.latchkey.gateway_client import FileSharingRequestPayload
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.gateway_client import LatchkeyGatewayClientError
from imbue.minds.desktop_client.latchkey.gateway_client import REQUEST_TYPE_FILE_SHARING
from imbue.minds.desktop_client.latchkey.gateway_client import StreamedPermissionRequest
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.recovery import maybe_recover_host_permissions
from imbue.minds.desktop_client.latchkey.handlers.resolution import resolve_request
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.minds.desktop_client.latchkey.response_events import RequestStatus
from imbue.minds.desktop_client.request_handler import RequestDetailPayload
from imbue.minds.desktop_client.request_handler import RequestEventHandler
from imbue.minds.desktop_client.request_handler import UiFileSharingPermissionDetail
from imbue.minds.desktop_client.request_handler import UiUnsupportedDetail
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_api_inbox import workspace_agent_id_for_request
from imbue.minds.desktop_client.ui_api_permissions import start_shared_path_sync
from imbue.minds.desktop_client.ui_api_permissions import workspace_sync_path_label
from imbue.minds.desktop_client.webdav import get_file_sharing_roots
from imbue.minds.errors import FolderSyncError
from imbue.mngr.primitives import AgentId
from imbue.mngr_latchkey.core import Latchkey

# Label shown on the inbox list card (lower-case, short).
_KIND_LABEL: Final[str] = "file sharing"

# Form field carrying the (possibly user-edited) absolute path to share.
# The dialog pre-fills it with the agent-requested path; the user may
# paste a different one or pick it from a native file dialog.
_FILE_PATH_FIELD: Final[str] = "file_path"

# Form fields carrying the dialog's sync choice: whether to also keep the
# granted folder synced, and which side wins a clash. Absent from an older
# client's post, which then grants without a sync.
_SYNC_FIELD: Final[str] = "sync"
_SYNC_CONFLICT_FIELD: Final[str] = "sync_conflict"


class _SyncChoice(FrozenModel):
    """What the dialog said about syncing, parsed from the grant form."""

    enabled: bool = Field(default=False, description="Whether the granted folder should also be kept synced")
    conflict: FolderSyncConflict = Field(
        default=FolderSyncConflict.NEWER, description="Which side wins a clash in a two-way sync"
    )


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


def _format_sync_started_note(workspace_path: str) -> str:
    return f" A synchronized copy of it is being kept on your machine at {workspace_path}."


def _format_sync_declined_note() -> str:
    return " The synchronized copy you asked for was not enabled."


def _parse_sync_choice(form: Mapping[str, str]) -> _SyncChoice:
    """The dialog's sync choice, or the no-sync default when the form carries none.

    Raises :class:`ValidationError` for a value the enums do not know: the
    dialog only ever sends values it was given, so anything else is a client
    the server does not understand rather than a choice to guess at.
    """
    raw: dict[str, str] = {}
    if _SYNC_FIELD in form:
        raw["enabled"] = form[_SYNC_FIELD]
    if _SYNC_CONFLICT_FIELD in form:
        raw["conflict"] = form[_SYNC_CONFLICT_FIELD]
    return _SyncChoice.model_validate(raw)


def _format_denied_message(file_path: str, access: str) -> str:
    return f"Your {_access_human_label(access)} file-sharing permission request for '{file_path}' was denied."


class FileSharingGrantHandler(RequestEventHandler):
    """Handler for file-sharing permission requests.

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
        description="Nudges the request's chat with the verdict on resolution (see :mod:`.messaging`).",
    )
    latchkey: Latchkey = Field(
        description="Latchkey wrapper used to repair a host's missing canonical permissions file at grant time.",
    )
    push_permissions_to_machine: Callable[[str], None] = Field(
        description=(
            "Pushes the freshly-spliced policy to the workspace's own machine, blocking until it lands "
            "there, and raising MachineOperationError when it does not. A no-op for a workspace whose "
            "agents run on this computer."
        ),
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

    def handles_request_type(self) -> str:
        return REQUEST_TYPE_FILE_SHARING

    def kind_label(self) -> str:
        return _KIND_LABEL

    def display_name_for_event(self, permission_request: StreamedPermissionRequest) -> str:
        payload = permission_request.payload
        if not isinstance(payload, FileSharingRequestPayload):
            return ""
        return payload.path

    def build_request_detail_payload(
        self,
        permission_request: StreamedPermissionRequest,
        backend_resolver: BackendResolverInterface,
    ) -> RequestDetailPayload:
        payload = permission_request.payload
        if not isinstance(payload, FileSharingRequestPayload):
            return UiUnsupportedDetail(message="Unsupported request type")
        parsed_agent_id = AgentId(permission_request.agent_id)
        ws_name = resolve_workspace_display_name(
            backend_resolver, parsed_agent_id, fallback=permission_request.agent_id
        )
        # The sync option is drawn from the same answers the Local files pane
        # gets, so the dialog greys out exactly the paths the pane would. A sync
        # belongs to the workspace, not to the chat that asked, so it is keyed
        # by the workspace's primary agent like the pane's own.
        manager = get_state().folder_sync_manager
        workspace_agent_id = workspace_agent_id_for_request(permission_request, backend_resolver)
        return UiFileSharingPermissionDetail(
            request_id=permission_request.request_id,
            agent_id=permission_request.agent_id,
            ws_name=ws_name,
            rationale=permission_request.rationale,
            file_path=payload.path,
            access=str(payload.access),
            access_human_label=_access_human_label(str(payload.access)),
            allowed_roots=tuple(str(root) for root in self.share_roots),
            home_dir=str(self.home_dir),
            is_sync_supported=manager is not None,
            is_sync_requested=payload.sync is not None,
            sync_conflict=FolderSyncConflict.NEWER if payload.sync is None else payload.sync.conflict,
            sync_unavailable_reason=(
                "" if manager is None else manager.reason_sync_is_unavailable(workspace_agent_id, payload.path)
            ),
        )

    def apply_grant_request(
        self,
        request: Request,
        permission_request: StreamedPermissionRequest,
    ) -> Response:
        payload = permission_request.payload
        if not isinstance(payload, FileSharingRequestPayload):
            return make_json_error_response("Unsupported request type", status_code=500)
        request_event_id = permission_request.request_id
        parsed_agent_id = AgentId(permission_request.agent_id)
        # A host whose canonical permissions file was never materialized must
        # be repaired before the grant, or the approval lands in a file the
        # agent's gateway JWT does not resolve to.
        maybe_recover_host_permissions(self.latchkey, get_state().backend_resolver, permission_request)

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
                else payload.path
            )
        except InvalidSharePathError as e:
            return make_json_error_response(str(e), status_code=400)
        try:
            sync_choice = _parse_sync_choice(form)
        except ValidationError as e:
            logger.debug("Rejected a malformed sync choice on a file-sharing grant: {}", e)
            return make_json_error_response("sync must be a boolean and sync_conflict a known value.", status_code=400)
        # A sync that cannot be started is refused before anything is granted:
        # the request stays pending with the reason, and the user can untick the
        # option or point at another folder and approve again. Refusing after
        # the grant would leave a permission the dialog never reported.
        manager: FolderSyncManager | None = None
        # The sync is the workspace's, keyed by its primary agent as the Local
        # files pane keys it; the grant itself stays with the agent that asked.
        workspace_agent_id = workspace_agent_id_for_request(permission_request, get_state().backend_resolver)
        if sync_choice.enabled:
            manager = get_state().folder_sync_manager
            if manager is None:
                return make_json_error_response(
                    "Keeping a shared path in sync is unavailable in this build.", status_code=400
                )
            unavailable_reason = manager.reason_sync_is_unavailable(workspace_agent_id, effective_path)
            if unavailable_reason:
                return make_json_error_response(unavailable_reason, status_code=400)

        # Only send an override to the gateway when the user actually
        # changed the path; otherwise the gateway applies the precomputed
        # effect verbatim (and we avoid recomputation for the common case).
        override_path = effective_path if effective_path != payload.path else None
        try:
            self.gateway_client.approve_permission_request(
                request_event_id,
                override_body={"path": override_path} if override_path is not None else None,
            )
        except LatchkeyGatewayClientError as e:
            logger.warning(
                "Could not approve file-sharing request {} via gateway: {}",
                request_event_id,
                e,
            )
            return make_json_error_response(
                f"Could not approve file-sharing request through the latchkey gateway: {e}",
                status_code=502,
            )
        # A remote workspace's machine enforces its own copy of the policy the
        # gateway just spliced the grant into, so the edit is pushed to it
        # before the grant is called done. A machine that will not take it
        # leaves the request unresolved: the reader is told what happened, and
        # the agent is left to ask again rather than being told it may read a
        # file its gateway will not let it read.
        try:
            self.push_permissions_to_machine(permission_request.agent_id)
        except MachineOperationError as e:
            logger.warning(
                "Could not apply the file-sharing grant on the machine of {}: {}", permission_request.agent_id, e
            )
            return make_json_error_response(str(e), status_code=502)

        message = _format_granted_message(effective_path, str(payload.access)) + self._start_sync_if_asked(
            manager, permission_request, workspace_agent_id, effective_path, sync_choice
        )
        resolve_request(
            self.mngr_message_sender,
            self.data_dir,
            request_event_id=request_event_id,
            agent_id=parsed_agent_id,
            status=RequestStatus.GRANTED,
            message=message,
        )
        return make_response(
            content=json.dumps({"outcome": "GRANTED", "message": message}),
            media_type="application/json",
        )

    def apply_deny_request(
        self,
        request: Request,
        permission_request: StreamedPermissionRequest,
    ) -> Response:
        payload = permission_request.payload
        if not isinstance(payload, FileSharingRequestPayload):
            return make_json_error_response("Unsupported request type", status_code=500)
        request_event_id = permission_request.request_id
        parsed_agent_id = AgentId(permission_request.agent_id)
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

        message = _format_denied_message(payload.path, str(payload.access))
        resolve_request(
            self.mngr_message_sender,
            self.data_dir,
            request_event_id=request_event_id,
            agent_id=parsed_agent_id,
            status=RequestStatus.DENIED,
            message=message,
        )
        return make_response(
            content=json.dumps({"outcome": "DENIED", "message": message}),
            media_type="application/json",
        )

    def _start_sync_if_asked(
        self,
        manager: FolderSyncManager | None,
        permission_request: StreamedPermissionRequest,
        workspace_agent_id: str,
        file_path: str,
        sync_choice: _SyncChoice,
    ) -> str:
        """Start the sync the dialog asked for, and say what became of the ask.

        Returns a sentence for the agent's notice: where the copy lands when a
        sync was started, that none was enabled when the agent asked for one
        and the user unticked it, and nothing at all otherwise. Starting is
        asynchronous, so the outcome is not known here; the Local files pane
        is where it shows up. Never raises: the grant has landed by now, and a
        sync that will not start must not undo it. The availability check
        before the grant makes that a race rather than the expected path.
        """
        payload = permission_request.payload
        is_requested = isinstance(payload, FileSharingRequestPayload) and payload.sync is not None
        if not sync_choice.enabled:
            return _format_sync_declined_note() if is_requested else ""
        if manager is None:
            return " Keeping it in sync was asked for but is unavailable in this build."
        try:
            status = start_shared_path_sync(manager, workspace_agent_id, file_path, sync_choice.conflict)
        except FolderSyncError as e:
            logger.warning("Granted file sharing for {} but could not start its sync: {}", file_path, e)
            return f" It is not being kept in sync: {e}"
        return _format_sync_started_note(workspace_sync_path_label(status.spec))
