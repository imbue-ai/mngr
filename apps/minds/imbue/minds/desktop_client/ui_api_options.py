"""/ui/api routes owned by tranche T3 (workspace options/settings).

One read endpoint serves everything the workspace options panel and the
standalone settings page render: ``GET /ui/api/workspaces/<agent_id>/options``.

Writes deliberately have no /ui twin here: rename, color, account
association, destroy, and the machine-sharing document all ride the existing
cookie-authed ``/api/v1`` routes, which already carry the concurrency story
those records support (sharing writes are whole-document replaces serialized
client-side; name/color/account are pass-throughs to mngr labels guarded by
mngr's own host/agent locks, so there is no version owned by Imbue Studio to If-Match).

The small context helpers here are the successors of ``app.py``'s private
``_build_workspace_context`` family, deleted with the legacy pages. The
share-target splitting and label resolution live in ``share_targets.py``,
shared with the sharing routes so every surface builds share links from one
label map.
"""

import json
import re
from collections.abc import Sequence
from typing import Final

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.ids import InvalidRandomIdError
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.connections.connection_store import ConnectionStore
from imbue.minds.desktop_client.connections.primitives import IMBUE_CLOUD_PROVIDER_NAME_PREFIX
from imbue.minds.desktop_client.connections.primitives import imbue_cloud_provider_name_for_user
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.machine_size import ImbueCloudMachineQuotaUsage
from imbue.minds.desktop_client.machine_size import ImbueCloudResizeBounds
from imbue.minds.desktop_client.machine_size import compute_imbue_cloud_resize_bounds
from imbue.minds.desktop_client.machine_size import is_imbue_cloud_machine_running
from imbue.minds.desktop_client.machine_size import is_imbue_cloud_machine_transitioning
from imbue.minds.desktop_client.machine_size import is_imbue_cloud_resize_offered_to_plan
from imbue.minds.desktop_client.machine_size import is_machine_disk_resizable_provider
from imbue.minds.desktop_client.machine_size import is_machine_resizable_provider
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.share_grant_validation import PUBLIC_EMAIL_DOMAINS
from imbue.minds.desktop_client.share_targets import resolve_share_target_display_names
from imbue.minds.desktop_client.share_targets import resolve_share_target_labels
from imbue.minds.desktop_client.share_targets import resolve_share_targets
from imbue.minds.desktop_client.state import DesktopClientState
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.ui_api_lifecycle import build_ssh_command
from imbue.minds.desktop_client.ui_auth import is_ui_request_authenticated
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import WORKSPACE_PALETTE
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationKind
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationRegistryInterface
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationStatus
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import is_account_synced_provider
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_imbue_cloud.wire import validate_wire
from imbue.mngr_imbue_cloud.wire_types import AccountInfo
from imbue.mngr_imbue_cloud.wire_types import WorkspaceStatus

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
    is_account_owned_provider: bool = Field(
        description="Whether the workspace runs on an account-owned provider (the account's imbue_cloud instance or "
        "one of its connections), which fixes the account link"
    )
    leased_owner_email: str = Field(
        default="",
        description="The account an account-owned provider's workspace belongs to, '' when not account-owned or not known",
    )
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
    app_services: tuple[str, ...] = Field(description="Per-app share targets: the apps the workspace offers up front")
    system_services: tuple[str, ...] = Field(
        default=(),
        description="Per-app share targets the workspace calls system apps, listed under the Share tab's System group",
    )
    service_labels: dict[str, str] = Field(description="Public origin label per share target (absent = no label yet)")
    service_icons: dict[str, str] = Field(
        default_factory=dict,
        description="Registered SVG icon markup per app share target (absent = none registered)",
    )
    service_display_names: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "What users read for each per-app share target (absent = the service registered no display "
            "name, or the workspace predates the field; show its service name instead). The whole-machine "
            "target is deliberately absent: it is named for what it grants."
        ),
    )
    whole_service: str = Field(description="The share target name that grants the whole machine")
    public_email_domains: tuple[str, ...] = Field(
        description=(
            "The mail providers a domain grant may never name, sorted, so the share panel can "
            "refuse such an entry before it becomes a row"
        )
    )
    ssh_command: str = Field(description="Copy-pasteable SSH command for the machine's host, '' when unknown")


class WorkspaceMachineSizeData(FrozenModel):
    """The machine-size facts the settings page renders for a workspace, whatever its provider.

    ``is_available`` is False when the size cannot be shown; every size fact
    is then absent/None, only ``is_restart_in_progress`` is still answered,
    and ``is_read_failed`` tells a size this app failed to read (the
    imbue_cloud connector lookup failed) from one the provider does not
    report, so the page says which rather than rendering an error. The
    pending ``target_*`` facts exist only for imbue_cloud, whose resize
    applies at the machine's next restart.

    An imbue_cloud machine's size and its resize offer come from two connector
    reads of very different cost (``machines show`` against ``account show``),
    so the default answer carries the size alone with
    ``is_resize_offer_pending`` set, and a second read with
    ``include_resize_offer=1`` adds the offer.
    """

    is_available: bool = Field(description="Whether machine-size facts could be read for this workspace")
    is_read_failed: bool = Field(
        default=False,
        description=(
            "Whether an unavailable size is one this app failed to read (the imbue_cloud connector could not "
            "answer for the machine) rather than one the provider does not report"
        ),
    )
    cpu_count: int | None = Field(default=None, description="CPUs the machine has; None when unknown or not shown")
    memory_gb: float | None = Field(default=None, description="Memory in GB; None when unknown")
    disk_gb: float | None = Field(default=None, description="Data-disk size in GB; None when unknown or uncapped")
    is_resizable: bool = Field(default=False, description="Whether the settings page can change the machine's size")
    is_disk_resizable: bool = Field(default=False, description="Whether the data disk can be grown from the page")
    is_resize_offer_pending: bool = Field(
        default=False,
        description=(
            "Whether this answer carries the size but not yet whether the page may change it: a second read "
            "with include_resize_offer=1 answers that (imbue_cloud only; every other provider's offer is known "
            "from the provider alone)"
        ),
    )
    is_restart_in_progress: bool = Field(
        default=False,
        description=(
            "Whether this app is restarting the workspace's host right now (a host recovery is running), "
            "during which the page offers no second restart"
        ),
    )
    target_memory_gb: int | None = Field(
        default=None, description="Pending resize's memory target (applied at the next restart); None when none"
    )
    target_disk_gb: int | None = Field(
        default=None, description="Pending disk grow's GB target (applied at the next restart); None when none"
    )
    is_restart_needed_to_apply: bool = Field(
        default=False, description="Whether a pending size target exists that a restart would apply"
    )
    is_running: bool | None = Field(
        default=None,
        description="Whether the machine is running (so a restart would apply a pending size now); None when unknown",
    )
    is_transitioning: bool = Field(
        default=False,
        description="Whether the machine is mid stop or start, during which a resize is refused (imbue_cloud only)",
    )
    memory_choices_gb: tuple[int, ...] = Field(
        default=(),
        description=(
            "The RAM sizes an imbue_cloud machine may be set to within its account's quota, its current size "
            "included; empty for machines sized by free numbers (docker, lima)"
        ),
    )
    is_memory_capped_by_plan: bool = Field(
        default=False, description="Whether the plan's units quota, not the fleet's largest size, bounds the choices"
    )
    plan_memory_limit_gb: int | None = Field(
        default=None, description="The plan's RAM quota across running machines (imbue_cloud only)"
    )
    other_machines_memory_gb: int | None = Field(
        default=None, description="RAM the account's other running machines use against that quota"
    )
    max_disk_gb: int | None = Field(
        default=None,
        description="The largest data disk the machine may be grown to within its quota (imbue_cloud only)",
    )
    plan_disk_limit_gb: int | None = Field(
        default=None, description="The plan's data-disk quota across running + stopped machines (imbue_cloud only)"
    )
    other_machines_disk_gb: int | None = Field(
        default=None, description="Data-disk GB the account's other machines use against that quota"
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


def _leased_owner_email(
    current_account: AccountSession | None,
    provider_name: str | None,
    accounts: Sequence[AccountSession],
    connection_store: ConnectionStore | None,
) -> str:
    """The email of the account a workspace on an account-owned provider belongs to: its linked account, else the
    signed-in account whose Imbue Cloud instance or connection hosts it. '' when none names one."""
    if current_account is not None:
        return str(current_account.email)
    if provider_name is None:
        return ""
    found = (
        connection_store.find_active_connection_by_instance_name(provider_name)
        if connection_store is not None
        else None
    )
    owner_user_id = found[0] if found is not None else None
    for account in accounts:
        if imbue_cloud_provider_name_for_user(str(account.user_id)) == provider_name:
            return str(account.email)
        if owner_user_id is not None and str(account.user_id) == owner_user_id:
            return str(account.email)
    return ""


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
    is_account_owned_provider = info is not None and is_account_synced_provider(
        str(info.provider_name or ""), get_state().connection_store
    )
    stored_color = backend_resolver.get_workspace_color(parsed_agent_id)

    icons = {
        str(service): icon for service, icon in backend_resolver.list_service_icons_for_agent(parsed_agent_id).items()
    }
    share_targets = resolve_share_targets(backend_resolver, parsed_agent_id)
    service_icons = {
        service: accepted
        for service in share_targets.per_app_services
        if (accepted := accepted_service_icon(icons.get(service, "")))
    }

    data = WorkspaceOptionsData(
        agent_id=agent_id,
        host_id=_workspace_host_coordinate_for_options(backend_resolver, session_store, agent_id),
        name=name,
        color=stored_color if stored_color is not None else DEFAULT_WORKSPACE_COLOR,
        palette=dict(WORKSPACE_PALETTE),
        is_stale=is_stale,
        is_account_owned_provider=is_account_owned_provider,
        leased_owner_email=_leased_owner_email(
            current_account, info.provider_name, accounts, get_state().connection_store
        )
        if is_account_owned_provider and info is not None
        else "",
        has_account=current_account is not None,
        account_email=current_account.email if current_account else "",
        account_display_name=current_account.display_name if current_account else None,
        account_profile_picture_url=current_account.profile_picture_url if current_account else None,
        current_account=_account_entry(current_account) if current_account else None,
        accounts=tuple(_account_entry(account) for account in accounts),
        app_services=share_targets.app_services,
        system_services=share_targets.system_services,
        service_labels=resolve_share_target_labels(backend_resolver, parsed_agent_id),
        service_icons=service_icons,
        service_display_names=resolve_share_target_display_names(backend_resolver, parsed_agent_id),
        whole_service=share_targets.whole_service,
        public_email_domains=tuple(sorted(PUBLIC_EMAIL_DOMAINS)),
        ssh_command=build_ssh_command(backend_resolver, parsed_agent_id),
    )
    return make_response(content=data.model_dump_json(), status_code=200, media_type="application/json")


def _handle_workspace_machine_size(agent_id: str) -> Response:
    """The machine-size facts for a workspace on any provider.

    Served separately from the options data so the settings page renders
    immediately and the size loads lazily. A local or cloud-VM machine's size
    is what its provider recorded, as discovery carries it; an imbue_cloud
    machine's comes from ``mngr imbue_cloud machines show`` (a connector round
    trip), which also knows about a resize pending its next restart. The
    imbue_cloud vCPU count does not follow a resize, so it is not shown there.

    An imbue_cloud machine's answer carries only its size unless the query
    names ``include_resize_offer=1``: the offer needs the account's plan and
    usage, a connector read that can take tens of seconds, and the page shows
    the size while it waits for it.
    """
    if not is_ui_request_authenticated():
        return _json_error_response(401, "Not authenticated")
    try:
        parsed_agent_id = AgentId(agent_id)
    except InvalidRandomIdError:
        return _json_error_response(404, "Unknown workspace")
    is_resize_offer_included = request.args.get("include_resize_offer", "") == "1"

    state = get_state()
    backend_resolver = state.backend_resolver
    info = backend_resolver.get_agent_display_info(parsed_agent_id)
    host_id = _workspace_host_coordinate_for_options(backend_resolver, state.session_store, agent_id)
    provider_name = "" if info is None else (info.provider_name or "")
    if info is None or not host_id:
        size = WorkspaceMachineSizeData(is_available=False)
    elif provider_name.startswith(IMBUE_CLOUD_PROVIDER_NAME_PREFIX):
        size = _imbue_cloud_machine_size(state, agent_id, host_id, is_resize_offer_included)
    else:
        size = _recorded_machine_size(backend_resolver, HostId(host_id), provider_name)
    # Answered on every path, an unavailable size included: the page follows it
    # to know when a restart it dispatched is over.
    data = size.model_copy_update(
        to_update(
            size.field_ref().is_restart_in_progress,
            _is_host_recovery_running(state.workspace_operation_registry, parsed_agent_id),
        ),
    )
    return make_response(content=data.model_dump_json(), status_code=200, media_type="application/json")


def _is_host_recovery_running(registry: WorkspaceOperationRegistryInterface, agent_id: AgentId) -> bool:
    """Whether this app is restarting (or starting) the workspace's host right now."""
    operation = registry.get(agent_id)
    return (
        operation is not None
        and operation.kind == WorkspaceOperationKind.RECOVERY
        and operation.status == WorkspaceOperationStatus.RUNNING
    )


def _recorded_machine_size(
    backend_resolver: BackendResolverInterface, host_id: HostId, provider_name: str
) -> WorkspaceMachineSizeData:
    """The size discovery carries for the machine's host, with whether this page can change it."""
    size = backend_resolver.get_host_size(host_id)
    if size is None:
        return WorkspaceMachineSizeData(is_available=False)
    return WorkspaceMachineSizeData(
        is_available=True,
        cpu_count=size.cpu_count,
        memory_gb=size.memory_gb,
        disk_gb=size.disk_gb,
        is_resizable=is_machine_resizable_provider(provider_name),
        is_disk_resizable=is_machine_disk_resizable_provider(provider_name),
    )


def _imbue_cloud_machine_size(
    state: DesktopClientState, agent_id: str, host_id: str, is_resize_offer_included: bool
) -> WorkspaceMachineSizeData:
    """The connector's size facts for the leased machine at ``host_id``, read through the plugin (specs/slice-fleet).

    The size controls are offered only to accounts on a plan in
    ``IMBUE_CLOUD_RESIZE_PLAN_NAMES``, bounded by what the account's quotas
    would grant; when the account's plan and usage cannot be read, the size
    is shown read-only rather than hidden. The offer is computed only when
    asked for: it costs an ``account show`` round trip on top of the
    ``machines show`` the size needs, and the page reads the two separately.
    """
    # Every leased machine has a size, so one that cannot be shown is one that could not be read.
    unavailable = WorkspaceMachineSizeData(is_available=False, is_read_failed=True)
    session_store = state.session_store
    imbue_cloud_cli = state.imbue_cloud_cli
    account = session_store.get_account_for_workspace(agent_id) if session_store else None
    if account is None or imbue_cloud_cli is None:
        return unavailable
    machine = imbue_cloud_cli.show_machine(account.email, host_id)
    if machine is None:
        return unavailable
    status = WorkspaceStatus(machine.status)
    # One unit is one GiB of machine RAM.
    size = WorkspaceMachineSizeData(
        is_available=True,
        memory_gb=machine.memory_units,
        disk_gb=machine.disk_gb,
        target_memory_gb=machine.target_memory_units,
        target_disk_gb=machine.target_disk_gb,
        is_restart_needed_to_apply=machine.is_restart_needed_to_apply,
        is_running=is_imbue_cloud_machine_running(status),
        is_transitioning=is_imbue_cloud_machine_transitioning(status),
    )
    if machine.memory_units is None or machine.disk_gb is None:
        return size
    if not is_resize_offer_included:
        return size.model_copy_update(to_update(size.field_ref().is_resize_offer_pending, True))
    account_info = _imbue_cloud_account_info(imbue_cloud_cli, str(account.email))
    if account_info is None or not is_imbue_cloud_resize_offered_to_plan(account_info.plan_name):
        return size
    quotas = _machine_quota_usage(account_info)
    if quotas is None:
        return size
    bounds = compute_imbue_cloud_resize_bounds(
        status=status,
        memory_units=machine.memory_units,
        target_memory_units=machine.target_memory_units,
        disk_gb=machine.disk_gb,
        target_disk_gb=machine.target_disk_gb,
        quotas=quotas,
    )
    return _with_resize_offer(size, bounds)


def _imbue_cloud_account_info(imbue_cloud_cli: ImbueCloudCli, email: str) -> AccountInfo | None:
    """The account's plan, quotas and usage, or None when the connector cannot be read (the size stays read-only)."""
    try:
        raw_info = imbue_cloud_cli.get_account_info(email)
    except ImbueCloudCliError as exc:
        logger.debug("Could not read the plan and usage of {} for the machine-size controls: {}", email, exc)
        return None
    return validate_wire(AccountInfo, raw_info)


@pure
def _machine_quota_usage(account_info: AccountInfo) -> ImbueCloudMachineQuotaUsage | None:
    """The machine quotas with their usage, or None against a connector too old to report them."""
    entitlements = account_info.entitlements
    usage = account_info.usage
    if (
        entitlements.max_active_machine_units is None
        or entitlements.max_total_machine_disk_gb is None
        or usage.active_machine_units is None
        or usage.total_machine_disk_gb is None
    ):
        return None
    return ImbueCloudMachineQuotaUsage(
        max_active_machine_units=entitlements.max_active_machine_units,
        active_machine_units=usage.active_machine_units,
        max_total_machine_disk_gb=entitlements.max_total_machine_disk_gb,
        total_machine_disk_gb=usage.total_machine_disk_gb,
    )


@pure
def _with_resize_offer(size: WorkspaceMachineSizeData, bounds: ImbueCloudResizeBounds) -> WorkspaceMachineSizeData:
    return size.model_copy_update(
        to_update(size.field_ref().is_resizable, True),
        to_update(size.field_ref().is_disk_resizable, True),
        to_update(size.field_ref().memory_choices_gb, bounds.memory_choices_gb),
        to_update(size.field_ref().is_memory_capped_by_plan, bounds.is_memory_capped_by_plan),
        to_update(size.field_ref().plan_memory_limit_gb, bounds.plan_memory_limit_gb),
        to_update(size.field_ref().other_machines_memory_gb, bounds.other_machines_memory_gb),
        to_update(size.field_ref().max_disk_gb, bounds.max_disk_gb),
        to_update(size.field_ref().plan_disk_limit_gb, bounds.plan_disk_limit_gb),
        to_update(size.field_ref().other_machines_disk_gb, bounds.other_machines_disk_gb),
    )


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
