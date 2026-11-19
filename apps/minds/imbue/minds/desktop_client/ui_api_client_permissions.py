"""/ui/api routes for client permissions: this computer's decisions about what a workspace's pages may use of it.

Three routes (``specs/workspace-microphone-permission/spec.md``, section 5.6),
all answered from this process alone: nothing here reads the workspace's
machine or the latchkey gateway, which is what lets the Permissions tab's
Client permissions section render while the rest of the tab cannot load (D10).
A build with no store (no data directory) answers all three with 503.

``GET /ui/api/client-permissions/decision`` is the Electron gate's one read,
made between a page asking for the microphone and the handler's answer; it is
a locked in-memory lookup plus the workspace's display name, which the gate
needs for its denial toast and so gets here rather than with a second request.
``GET .../workspaces/<agent_id>/client-permissions`` is the section's payload,
one row per declared permission. ``PUT .../client-permissions/<name>`` stores,
changes, or (with ``null``) deletes the workspace's own entry and answers with
the refreshed section, so the pane renders what was written rather than what
it guessed.

A workspace's decision is resolved through the settings model's one order
(``settings/resolve.py``): the workspace's own entry, else the install's value,
else the Imbue default. The copy for each permission lives here, with the
routes that serve it, because it is what the pane says rather than what the
setting is.
"""

from collections.abc import Mapping
from typing import Final
from typing import assert_never

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.minds.desktop_client.backend_resolver import resolve_workspace_display_name
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.responses import make_json_error_response
from imbue.minds.desktop_client.settings.resolve import ResolvedClientPermission
from imbue.minds.desktop_client.settings.resolve import SettingsResolutionContext
from imbue.minds.desktop_client.settings.resolve import resolve_client_permission
from imbue.minds.desktop_client.settings.schema import ClientPermissionDecision
from imbue.minds.desktop_client.settings.schema import ClientPermissionName
from imbue.minds.desktop_client.settings.schema import ClientPermissionSource
from imbue.minds.desktop_client.settings.storage import load_settings_context
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_api_permissions import json_response
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.ui_models import UiClientPermissionDecision
from imbue.minds.desktop_client.ui_models import UiClientPermissionRow
from imbue.minds.desktop_client.ui_models import UiClientPermissionWriteRequest
from imbue.minds.desktop_client.ui_models import UiClientPermissions
from imbue.minds.desktop_client.workspace_install_settings_store import WorkspaceInstallSettingsStore
from imbue.minds.errors import WorkspaceInstallSettingsStoreError
from imbue.minds.errors import WorkspaceInstallSettingsTooNewError
from imbue.mngr.primitives import AgentId


class ClientPermissionCopy(FrozenModel):
    """What the pane says about one client permission: its row label and what granting it lets a page do."""

    label: str = Field(description="Row label")
    description: str = Field(description="What granting the permission lets a page of the workspace do")


# One entry per declared permission.
CLIENT_PERMISSION_COPY_BY_NAME: Final[Mapping[ClientPermissionName, ClientPermissionCopy]] = {
    ClientPermissionName.MICROPHONE: ClientPermissionCopy(
        label="Microphone",
        description="Listen to an audio input: the built-in mic, a headset, a USB interface.",
    ),
}


def _parse_client_permission_name(raw: str) -> ClientPermissionName | None:
    """The declared permission ``raw`` names, or None for one this build does not declare."""
    try:
        return ClientPermissionName(raw)
    except ValueError:
        return None


def _store_or_unavailable() -> WorkspaceInstallSettingsStore | Response:
    """The store this build keeps decisions in, or the 503 a build without one answers every route with."""
    store = get_state().workspace_install_settings_store
    if store is None:
        return make_json_error_response("Client permissions are not available in this build.", 503)
    return store


def _settings_context_for_workspace(
    agent_id: AgentId, store: WorkspaceInstallSettingsStore
) -> SettingsResolutionContext:
    """The resolution context for one workspace on this computer.

    Client permissions are install-keyed, so no account's record takes part in
    the answer; the device's pseudo-account stands in for the context's
    account, as it does for every read the desktop makes for itself.
    """
    state = get_state()
    return load_settings_context(
        state.minds_config,
        state.account_settings_store,
        LOCAL_PROFILE_USER_ID,
        workspace_id=agent_id,
        workspace_install_settings_store=store,
    )


def _stored_entry(resolved: ResolvedClientPermission) -> ClientPermissionDecision | None:
    """The workspace's own entry, read off the resolution: only a workspace-sourced decision is one."""
    match resolved.source:
        case ClientPermissionSource.WORKSPACE:
            return resolved.decision
        case ClientPermissionSource.DEFAULT:
            return None
        case _ as unreachable:
            assert_never(unreachable)


def build_client_permissions_payload(agent_id: AgentId, store: WorkspaceInstallSettingsStore) -> UiClientPermissions:
    """The section's payload: every declared permission, resolved for this workspace on this computer."""
    context = _settings_context_for_workspace(agent_id, store)
    rows: list[UiClientPermissionRow] = []
    for name in ClientPermissionName:
        resolved = resolve_client_permission(name, context)
        copy = CLIENT_PERMISSION_COPY_BY_NAME[name]
        rows.append(
            UiClientPermissionRow(
                permission=name.value,
                label=copy.label,
                description=copy.description,
                decision=resolved.decision,
                source=resolved.source,
                stored=_stored_entry(resolved),
            )
        )
    return UiClientPermissions(rows=tuple(rows))


def _handle_client_permission_decision() -> Response:
    """GET /ui/api/client-permissions/decision?workspace=<agent id>&permission=<name>: the gate's one read.

    400 for a ``workspace`` that is not an agent id; a workspace the resolver
    does not know is still answered, named by its id, since the decision is
    this computer's and needs nothing from the workspace. 404 for a permission
    this build does not declare.
    """
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", 401)
    store = _store_or_unavailable()
    if isinstance(store, Response):
        return store
    raw_workspace = request.args.get("workspace", "")
    try:
        agent_id = AgentId(raw_workspace)
    except InvalidRandomIdError:
        return make_json_error_response("The workspace parameter must name a workspace by its agent id", 400)
    raw_permission = request.args.get("permission", "")
    name = _parse_client_permission_name(raw_permission)
    if name is None:
        return make_json_error_response(f"Unknown client permission '{raw_permission}'", 404)
    resolved = resolve_client_permission(name, _settings_context_for_workspace(agent_id, store))
    return json_response(
        UiClientPermissionDecision(
            decision=resolved.decision,
            source=resolved.source,
            workspace_name=resolve_workspace_display_name(
                get_state().backend_resolver, agent_id, fallback=str(agent_id)
            ),
        )
    )


class _WorkspaceRouteTarget(FrozenModel):
    """What the two workspace routes act on once their prelude passes."""

    agent_id: AgentId = Field(description="The workspace the route names")
    store: WorkspaceInstallSettingsStore = Field(description="Where this build keeps the workspace's decisions")


def _workspace_prelude(agent_id: str) -> Response | _WorkspaceRouteTarget:
    """Shared by the two workspace routes: 401 unauthenticated, 503 without a store, 404 malformed id."""
    if not is_ui_request_authenticated():
        return make_json_error_response("Not authenticated", 401)
    store = _store_or_unavailable()
    if isinstance(store, Response):
        return store
    try:
        parsed_id = AgentId(agent_id)
    except InvalidRandomIdError:
        return make_json_error_response("Unknown workspace", 404)
    return _WorkspaceRouteTarget(agent_id=parsed_id, store=store)


def _handle_client_permissions(agent_id: str) -> Response:
    """GET /ui/api/workspaces/<agent_id>/client-permissions: the Client permissions section's payload."""
    prelude = _workspace_prelude(agent_id)
    if isinstance(prelude, Response):
        return prelude
    return json_response(build_client_permissions_payload(prelude.agent_id, prelude.store))


def _handle_client_permission_write(agent_id: str, name: str) -> Response:
    """PUT /ui/api/workspaces/<agent_id>/client-permissions/<name>: set or clear the workspace's own entry.

    A ``null`` decision deletes the entry, so the workspace defers to the next
    level again (spec D3). 404 for a permission this build does not declare,
    400 for a body that is not ``{"decision": ...}``, 409 for a record a newer
    build wrote (readable, never rewritten).
    """
    prelude = _workspace_prelude(agent_id)
    if isinstance(prelude, Response):
        return prelude
    permission = _parse_client_permission_name(name)
    if permission is None:
        return make_json_error_response(f"Unknown client permission '{name}'", 404)
    body = request.get_json(silent=True, force=True)
    if not isinstance(body, dict):
        return make_json_error_response("Invalid JSON body", 400)
    try:
        write_request = UiClientPermissionWriteRequest.model_validate(body)
    except ValidationError as e:
        logger.debug("Rejected a malformed client-permission body: {}", e)
        return make_json_error_response("decision is required and must be granted, denied, ask, or null.", 400)
    try:
        prelude.store.set_client_permission(str(prelude.agent_id), permission, write_request.decision)
    except WorkspaceInstallSettingsTooNewError as e:
        return make_json_error_response(str(e), 409)
    except WorkspaceInstallSettingsStoreError as e:
        logger.warning("Could not store the {} decision for workspace {}: {}", permission.value, prelude.agent_id, e)
        return make_json_error_response(str(e), 500)
    return json_response(build_client_permissions_payload(prelude.agent_id, prelude.store))


def register_client_permission_routes(blueprint: Blueprint) -> None:
    """Register the client-permission routes on the shared /ui blueprint."""
    blueprint.add_url_rule(
        "/api/client-permissions/decision",
        view_func=_handle_client_permission_decision,
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/client-permissions",
        view_func=_handle_client_permissions,
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/client-permissions/<name>",
        view_func=_handle_client_permission_write,
        methods=["PUT"],
    )
