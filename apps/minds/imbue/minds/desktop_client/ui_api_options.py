"""/ui/api routes owned by tranche T3 (workspace options/settings).

One read endpoint serves everything the workspace options panel and the
standalone settings page render: ``GET /ui/api/workspaces/<agent_id>/options``.

Writes deliberately have no /ui twin here: rename, color, account
association, destroy, and the machine-sharing document all ride the existing
cookie-authed ``/api/v1`` routes, which already carry the concurrency story
those records support (sharing writes are whole-document replaces serialized
client-side; name/color/account are pass-throughs to mngr labels guarded by
mngr's own host/agent locks, so there is no minds-owned version to If-Match).

The small context helpers here are the successors of ``app.py``'s private
``_build_workspace_context`` family, deleted with the legacy pages. The
share-target splitting and label resolution live in ``share_targets.py``,
shared with the sharing routes so every surface builds share links from one
label map.
"""

import json
import re
from typing import Final

from flask import Blueprint
from flask import Response
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.share_targets import resolve_share_target_labels
from imbue.minds.desktop_client.share_targets import split_share_targets
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import WORKSPACE_PALETTE
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.mngr.primitives import AgentId

# App icons are SVG markup authored inside the workspace -- untrusted content
# headed for the trusted shell's DOM. This is the server-side backstop
# (mirroring the workspace registry's own ``_accepted_icon`` in
# default-workspace-template's agent_manager.py); the frontend additionally
# DOMPurify-sanitizes before inlining.
_MAX_ICON_LENGTH: Final[int] = 16384
_FORBIDDEN_ICON_SUBSTRINGS: Final[tuple[str, ...]] = ("<script", "<style", "<foreignobject", "javascript:", "<!", "<?")
# An ``on*=`` attribute anywhere in a tag, e.g. ``<svg onload="...">``.
_ICON_EVENT_HANDLER_PATTERN: Final[re.Pattern[str]] = re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE)


@pure
def accepted_service_icon(raw_icon: str) -> str:
    """Return ``raw_icon`` when it is safe to serve as an app icon, else ''."""
    icon = raw_icon.strip()
    if not icon or len(icon) > _MAX_ICON_LENGTH:
        return ""
    if not icon.startswith("<svg") or not icon.endswith(">"):
        return ""
    lowered = icon.lower()
    if any(forbidden in lowered for forbidden in _FORBIDDEN_ICON_SUBSTRINGS):
        return ""
    if _ICON_EVENT_HANDLER_PATTERN.search(icon) is not None:
        return ""
    return icon


# Hosts leased from Imbue Cloud surface under per-account provider instances
# with this prefix; their account link is fixed.
_IMBUE_CLOUD_PROVIDER_PREFIX: Final[str] = "imbue_cloud_"


class WorkspaceOptionsAccount(FrozenModel):
    """One signed-in account as the options surfaces show it."""

    user_id: str = Field(description="SuperTokens user id")
    email: str = Field(description="Account email address")
    display_name: str | None = Field(default=None, description="Display name from the OAuth provider")


class WorkspaceOptionsData(FrozenModel):
    """Everything the workspace options panel + settings page render for one workspace."""

    agent_id: str = Field(description="The workspace's stable identity")
    host_id: str = Field(description="The machine's host-<hex> coordinate (keys the sharing API); '' when unknown")
    name: str = Field(description="Display name, falling back to the agent id")
    color: str = Field(description="Stored color hex, or the default for label-less workspaces")
    palette: dict[str, str] = Field(description="Pickable palette swatches, name -> hex")
    is_stale: bool = Field(description="Whether the owning provider's last discovery poll errored")
    is_leased_imbue_cloud: bool = Field(description="Whether the host lease fixes the account link")
    has_account: bool = Field(description="Whether the workspace is associated with an account")
    account_email: str = Field(description="The associated account's email, '' when unassociated")
    account_display_name: str | None = Field(
        default=None, description="The associated account's display name; None when unassociated or unset"
    )
    account_profile_picture_url: str | None = Field(
        default=None, description="Public URL of the associated account's profile picture; None when none"
    )
    current_account: WorkspaceOptionsAccount | None = Field(default=None, description="The associated account, if any")
    accounts: tuple[WorkspaceOptionsAccount, ...] = Field(description="Every signed-in account (Associate prompt)")
    app_services: tuple[str, ...] = Field(description="Per-app share targets (DNS-safe, non-interface services)")
    service_labels: dict[str, str] = Field(description="Public origin label per share target (absent = no label yet)")
    service_icons: dict[str, str] = Field(
        default_factory=dict,
        description="Registered SVG icon markup per app share target (absent = none registered)",
    )
    whole_service: str = Field(description="The share target name that grants the whole machine")


class WorkspaceMachineSizeData(FrozenModel):
    """The read-only machine-size facts the settings page renders for a leased machine.

    ``is_available`` is False when the size cannot be shown (not an
    imbue_cloud lease, no associated account, or the connector lookup
    failed); every other field is then absent/None and the page hides the
    section rather than rendering an error.
    """

    is_available: bool = Field(description="Whether machine-size facts could be fetched for this workspace")
    memory_units: int | None = Field(
        default=None, description="Current size in units (1 unit = 1GiB machine RAM); None when unknown"
    )
    target_memory_units: int | None = Field(
        default=None, description="Pending resize's unit target (applied at the next restart); None when none"
    )
    disk_gb: int | None = Field(default=None, description="Current data-disk size in GB; None when unknown")
    target_disk_gb: int | None = Field(
        default=None, description="Pending disk grow's GB target (applied at the next restart); None when none"
    )
    is_restart_needed_to_apply: bool = Field(
        default=False, description="Whether a pending size target exists that a restart would apply"
    )


def _recorded_workspace_name(session_store: MultiAccountSessionStore | None, agent_id: str) -> str:
    """The record-kept display name for a workspace discovery does not know (prefer active records)."""
    record_store = session_store.record_store if session_store else None
    if record_store is None:
        return agent_id
    fallback_name = ""
    for records in record_store.list_all_records().values():
        for record in records:
            if record.agent_id != agent_id or not record.display_name:
                continue
            if record.state == RECORD_STATE_ACTIVE:
                return record.display_name
            fallback_name = record.display_name
    return fallback_name or agent_id


def _workspace_host_coordinate_for_options(
    backend_resolver: BackendResolverInterface,
    session_store: MultiAccountSessionStore | None,
    agent_id: str,
) -> str:
    """The machine's host-<hex> coordinate, or '' when it cannot be determined."""
    info = backend_resolver.get_agent_display_info(AgentId(agent_id))
    if info is not None and str(info.host_id).startswith("host-"):
        return str(info.host_id)
    record_store = session_store.record_store if session_store else None
    if record_store is not None:
        found = record_store.find_active_record(agent_id)
        if found is not None and found[1].host_id.startswith("host-"):
            return found[1].host_id
    return ""


def _account_entry(account: AccountSession) -> WorkspaceOptionsAccount:
    return WorkspaceOptionsAccount(
        user_id=str(account.user_id),
        email=account.email,
        display_name=account.display_name,
    )


def _json_error_response(status_code: int, message: str) -> Response:
    return make_response(
        content=json.dumps({"error": message}), status_code=status_code, media_type="application/json"
    )


def _handle_workspace_options_data(agent_id: str) -> Response:
    if not is_ui_request_authenticated():
        return _json_error_response(401, "Not authenticated")
    try:
        parsed_agent_id = AgentId(agent_id)
    except InvalidRandomIdError:
        return _json_error_response(404, "Unknown workspace")

    backend_resolver = get_state().backend_resolver
    session_store = get_state().session_store
    current_account = session_store.get_account_for_workspace(agent_id) if session_store else None
    accounts = session_store.list_accounts() if session_store else []

    info = backend_resolver.get_agent_display_info(parsed_agent_id)
    name = backend_resolver.get_workspace_name(parsed_agent_id) or ""
    if not name and info is not None:
        name = info.agent_name
    if not name:
        name = _recorded_workspace_name(session_store, agent_id)

    errored_provider_names = {str(provider) for provider in backend_resolver.get_provider_errors()}
    is_stale = info is not None and info.provider_name is not None and info.provider_name in errored_provider_names
    is_leased = info is not None and (info.provider_name or "").startswith(_IMBUE_CLOUD_PROVIDER_PREFIX)
    stored_color = backend_resolver.get_workspace_color(parsed_agent_id)

    services = [str(service) for service in backend_resolver.list_services_for_agent(parsed_agent_id)]
    icons = {
        str(service): icon for service, icon in backend_resolver.list_service_icons_for_agent(parsed_agent_id).items()
    }
    app_services, whole_service = split_share_targets(services)
    service_icons = {
        service: accepted for service in app_services if (accepted := accepted_service_icon(icons.get(service, "")))
    }

    data = WorkspaceOptionsData(
        agent_id=agent_id,
        host_id=_workspace_host_coordinate_for_options(backend_resolver, session_store, agent_id),
        name=name,
        color=stored_color if stored_color is not None else DEFAULT_WORKSPACE_COLOR,
        palette=dict(WORKSPACE_PALETTE),
        is_stale=is_stale,
        is_leased_imbue_cloud=is_leased,
        has_account=current_account is not None,
        account_email=current_account.email if current_account else "",
        account_display_name=current_account.display_name if current_account else None,
        account_profile_picture_url=current_account.profile_picture_url if current_account else None,
        current_account=_account_entry(current_account) if current_account else None,
        accounts=tuple(_account_entry(account) for account in accounts),
        app_services=tuple(app_services),
        service_labels=resolve_share_target_labels(backend_resolver, parsed_agent_id),
        service_icons=service_icons,
        whole_service=whole_service,
    )
    return make_response(content=data.model_dump_json(), status_code=200, media_type="application/json")


def _handle_workspace_machine_size(agent_id: str) -> Response:
    """The read-only machine-size facts for a leased imbue_cloud workspace (specs/slice-fleet).

    Served separately from the options data so the settings page renders
    immediately and the size loads lazily -- fetching it costs a
    ``mngr imbue_cloud machines show`` round trip to the connector.
    """
    if not is_ui_request_authenticated():
        return _json_error_response(401, "Not authenticated")
    try:
        parsed_agent_id = AgentId(agent_id)
    except InvalidRandomIdError:
        return _json_error_response(404, "Unknown workspace")

    unavailable = WorkspaceMachineSizeData(is_available=False)
    state = get_state()
    session_store = state.session_store
    imbue_cloud_cli = state.imbue_cloud_cli
    backend_resolver = state.backend_resolver
    info = backend_resolver.get_agent_display_info(parsed_agent_id)
    is_leased = info is not None and (info.provider_name or "").startswith(_IMBUE_CLOUD_PROVIDER_PREFIX)
    account = session_store.get_account_for_workspace(agent_id) if session_store else None
    host_id = _workspace_host_coordinate_for_options(backend_resolver, session_store, agent_id)
    if not is_leased or account is None or imbue_cloud_cli is None or not host_id:
        return make_response(content=unavailable.model_dump_json(), status_code=200, media_type="application/json")

    machine = imbue_cloud_cli.show_machine(account.email, host_id)
    if machine is None:
        return make_response(content=unavailable.model_dump_json(), status_code=200, media_type="application/json")
    data = WorkspaceMachineSizeData(
        is_available=True,
        memory_units=machine.memory_units,
        target_memory_units=machine.target_memory_units,
        disk_gb=machine.disk_gb,
        target_disk_gb=machine.target_disk_gb,
        is_restart_needed_to_apply=machine.is_restart_needed_to_apply,
    )
    return make_response(content=data.model_dump_json(), status_code=200, media_type="application/json")


def register_options_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/options",
        view_func=_handle_workspace_options_data,
        methods=["GET"],
    )
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/machine-size",
        view_func=_handle_workspace_machine_size,
        methods=["GET"],
    )
