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
``_build_workspace_context`` family and ``templates.py``'s share-target
splitters, both deleted with the legacy pages; this module is their single
home.
"""

import json
import os
import re
from collections.abc import Sequence
from typing import Final

from flask import Blueprint
from flask import Response
from flask import request
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import WORKSPACE_PALETTE
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.mngr.primitives import AgentId

# The share target that grants the whole machine (the shell service).
WHOLE_MACHINE_SERVICE: Final[str] = "system_interface"

# Interfaces the workspace is built out of (or internal infrastructure) rather
# than apps built on top of it: excluded from the per-app share targets (the
# whole machine remains the deliberate way to grant everything). ``owner-exec``
# is the internal SSH-equivalent exec channel (authorized by request signatures
# against authorized_keys, never a share grant), so it must never be offered as
# a per-app share target.
_NON_APP_SHARE_SERVICES: Final[frozenset[str]] = frozenset(
    {"chat", "chats", "terminal", "terminals", "browser", "browsers", "owner-exec"}
)

# A per-app share link is a real origin, so only DNS-label-safe names qualify.
_DNS_SAFE_SERVICE_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

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
    current_account: WorkspaceOptionsAccount | None = Field(default=None, description="The associated account, if any")
    accounts: tuple[WorkspaceOptionsAccount, ...] = Field(description="Every signed-in account (Associate prompt)")
    app_services: tuple[str, ...] = Field(description="Per-app share targets (DNS-safe, non-interface services)")
    service_labels: dict[str, str] = Field(description="Public origin label per share target (absent = no label yet)")
    whole_service: str = Field(description="The share target name that grants the whole machine")


def _is_options_request_authenticated() -> bool:
    """The same signed-cookie check as ui_api.is_ui_request_authenticated.

    Local twin because ui_api imports this module (registration), so importing
    back would be circular; a shared guard hoisted onto the /ui blueprint
    would remove the duplication.
    """
    if os.getenv("SKIP_AUTH", "0") == "1":
        return True
    signing_key = get_state().auth_store.get_signing_key()
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value is None:
        return False
    return verify_session_cookie(cookie_value=cookie_value, signing_key=signing_key)


@pure
def split_share_targets(servers: Sequence[str]) -> tuple[list[str], str]:
    """Split a workspace's services into per-app share targets and the whole-machine one.

    The whole-machine entry is always offered; interface services and names
    that cannot be a hostname label are excluded from the per-app list (they
    stay reachable through a whole-machine share).
    """
    app_services = [
        str(service)
        for service in servers
        if str(service) != WHOLE_MACHINE_SERVICE
        and str(service).lower() not in _NON_APP_SHARE_SERVICES
        and _DNS_SAFE_SERVICE_NAME.match(str(service)) is not None
        and not str(service).startswith(("host-", "agent-"))
    ]
    return app_services, WHOLE_MACHINE_SERVICE


@pure
def share_target_labels(app_services: Sequence[str], service_labels: dict[str, str]) -> dict[str, str]:
    """The origin-label map for the rendered share targets (services without a label are omitted)."""
    target_labels = {service: service_labels[service] for service in app_services if service in service_labels}
    if WHOLE_MACHINE_SERVICE in service_labels:
        target_labels[WHOLE_MACHINE_SERVICE] = service_labels[WHOLE_MACHINE_SERVICE]
    return target_labels


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
    if not _is_options_request_authenticated():
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
    labels = {
        str(service): label
        for service, label in backend_resolver.list_service_labels_for_agent(parsed_agent_id).items()
    }
    app_services, whole_service = split_share_targets(services)

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
        current_account=_account_entry(current_account) if current_account else None,
        accounts=tuple(_account_entry(account) for account in accounts),
        app_services=tuple(app_services),
        service_labels=share_target_labels(app_services, labels),
        whole_service=whole_service,
    )
    return make_response(content=data.model_dump_json(), status_code=200, media_type="application/json")


def register_options_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule(
        "/api/workspaces/<agent_id>/options",
        view_func=_handle_workspace_options_data,
        methods=["GET"],
    )
