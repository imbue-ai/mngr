"""/ui/api routes owned by tranche T4 (backups/destroy/recovery).

Three surfaces:

- ``GET /ui/api/destroyed-workspaces`` -- the JSON twin of the deleted
  ``/workspaces/destroyed/rows`` HTML fragment: tombstoned records still in
  the backup retention window (newest destroyed first) plus orphan backup
  envs this device holds, with the retention window for the page header.
- ``POST /ui/api/destroyed-workspaces/<agent_id>/delete-backup`` -- frees a
  destroyed workspace's backup quota now instead of waiting out retention.
- ``GET /ui/api/workspaces/<workspace_id>/recovery-info`` -- everything the
  Recovery card needs beyond the live channel health state: the resolved
  agent id (either workspace coordinate is accepted, since restored windows
  navigate host-keyed), the display name, the copy-pasteable SSH command,
  whether the host currently reads as offline, and whether the machine's
  backend is unreachable (with the provider's own reason). The card polls
  this while it is open, so every field is a current reading rather than a
  one-shot snapshot taken when it opened.

This module is the single home of the destroyed-row derivation (moved here
from ``app.py``'s deleted legacy page handlers).
"""

import json
import os
from collections.abc import Iterable
from collections.abc import Iterator
from datetime import datetime
from datetime import timezone

from flask import Blueprint
from flask import Response
from flask import request
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_reaper import BACKUP_RETENTION_FALLBACK_SECONDS
from imbue.minds.desktop_client.backup_reaper import BackupReaperManager
from imbue.minds.desktop_client.backup_reaper import ReapCandidate
from imbue.minds.desktop_client.backup_reaper import bucket_owner_prefix_from_env
from imbue.minds.desktop_client.backup_reaper import emails_by_bucket_owner_prefix
from imbue.minds.desktop_client.backup_reaper import list_orphan_env_agent_ids
from imbue.minds.desktop_client.backup_reaper import parse_destroyed_at
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.environment_signals import EnvironmentBlock
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_DESTROYED
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.desktop_client.workspace_recovery import read_backend_unreachable_verdict
from imbue.minds.desktop_client.workspace_recovery import read_device_cannot_connect_verdict
from imbue.minds.desktop_client.workspace_recovery import read_environment_block
from imbue.minds.desktop_client.workspace_recovery import read_host_state
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostState

_SECONDS_PER_DAY: float = 86400.0


class DestroyedWorkspaceRow(FrozenModel):
    """One row of the recently-destroyed page: a tombstoned record or an orphan backup env."""

    agent_id: str = Field(description="The destroyed workspace's stable identity")
    display_name: str = Field(description="Human-readable workspace name (or an unknown-workspace label)")
    account_label: str = Field(description="Owning account email, or 'this device' for orphans")
    destroyed_at_display: str = Field(description="Destruction date (YYYY-MM-DD), empty for orphans")
    days_left_display: str = Field(description="Retention countdown copy, empty when unknown")
    has_backup: bool = Field(description="Whether any backup material exists for the row")
    can_download: bool = Field(description="Whether the latest-snapshot export can run from this device")
    is_locked: bool = Field(description="Backup exists only as encrypted secrets (sync password locked)")
    can_delete: bool = Field(description="Whether the delete-backup action is offered")
    delete_hint: str = Field(description="Why delete is unavailable, when it is")


class DestroyedWorkspacesResponse(FrozenModel):
    """Payload for GET /ui/api/destroyed-workspaces."""

    retention_days: int = Field(description="How long destroyed-workspace backups are kept")
    rows: tuple[DestroyedWorkspaceRow, ...] = Field(description="Newest-destroyed first, orphans last")


class RecoveryInfoResponse(FrozenModel):
    """Payload for GET /ui/api/workspaces/<workspace_id>/recovery-info."""

    agent_id: str = Field(description="The workspace's resolved agent id (input may be host-keyed)")
    workspace_name: str = Field(description="Display name, falling back to the agent id")
    health: AgentHealth = Field(description="Current tracker health state")
    health_error: str = Field(description="Last restart error, empty when none")
    is_restart_start_only: bool | None = Field(
        description="Whether an in-flight restart skips the stop step; None outside a restart"
    )
    ssh_command: str = Field(description="Copy-pasteable SSH command for the host, empty when unknown")
    is_host_offline: bool = Field(description="Whether discovery currently reads the host as stopped/stopping/crashed")
    device_environment: EnvironmentBlock = Field(
        description=(
            "Device-level condition blocking this machine (offline / SSH-blocked network), or NONE. "
            "Outranks the machine's own health in the card: while it holds there is no restart to "
            "narrate and none that could run. The device's cached reading, not a per-machine "
            "record, so it answers for a machine that was already stuck when the network died and "
            "for one whose restart already failed. Still NONE for a machine that runs on this "
            "device: a docker container is reachable with the wifi off, so a dead network neither "
            "explains its failure nor is a reason to withhold the restart that fixes it."
        )
    )
    is_backend_unreachable: bool = Field(
        description="Whether the provider hosting this machine is unreachable or rejecting us"
    )
    provider_label: str = Field(description="Friendly provider name, empty unless the backend is unreachable")
    unreachable_reason: str = Field(description="The provider's own error, empty unless the backend is unreachable")
    is_device_cannot_connect: bool = Field(
        description="Whether this device, rather than the machine, is what cannot make the connection"
    )
    device_error_detail: str = Field(
        description="The forward's verbatim error, empty unless this device is what cannot connect"
    )


def _is_lifecycle_request_authenticated() -> bool:
    """The /ui session check, local to this module to avoid a circular import via ui_api.

    Mirrors ``ui_api.is_ui_request_authenticated`` (including the SKIP_AUTH
    test escape hatch); a shared helper hoisted onto the /ui blueprint
    would remove the duplication.
    """
    if os.getenv("SKIP_AUTH", "0") == "1":
        return True
    signing_key = get_state().auth_store.get_signing_key()
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_value is None:
        return False
    return verify_session_cookie(cookie_value=cookie_value, signing_key=signing_key)


def _json_response(model: FrozenModel, status_code: int) -> Response:
    return Response(model.model_dump_json(), status=status_code, mimetype="application/json")


def _error_response(message: str, status_code: int) -> Response:
    return Response(json.dumps({"error": message}), status=status_code, mimetype="application/json")


def _signed_in_accounts_by_user_id() -> dict[str, str]:
    session_store = get_state().session_store
    if session_store is None:
        return {}
    return {str(account.user_id): str(account.email) for account in session_store.list_accounts()}


def _get_backup_reaper() -> BackupReaperManager | None:
    scheduler = get_state().sync_scheduler
    return scheduler.backup_reaper if scheduler is not None else None


def _retention_days() -> int:
    reaper = _get_backup_reaper()
    retention_seconds = reaper.get_retention_seconds() if reaper is not None else BACKUP_RETENTION_FALLBACK_SECONDS
    return max(1, round(retention_seconds / _SECONDS_PER_DAY))


def _days_left_display(destroyed_at: datetime | None, retention_seconds: float, now: datetime) -> str:
    if destroyed_at is None:
        return ""
    days_left = max(0, round((retention_seconds - (now - destroyed_at).total_seconds()) / _SECONDS_PER_DAY))
    return "deleting soon" if days_left <= 0 else f"{days_left} day(s) until deletion"


def _collect_destroyed_rows() -> list[DestroyedWorkspaceRow]:
    """Tombstoned records for every signed-in account (newest first), then orphan envs."""
    state = get_state()
    paths = state.api_v1_paths
    session_store = state.session_store
    record_store = session_store.record_store if session_store is not None else None
    if paths is None or record_store is None:
        return []
    accounts = _signed_in_accounts_by_user_id()
    retention_seconds = _retention_days() * _SECONDS_PER_DAY
    now = datetime.now(timezone.utc)

    # Tombstoned records still inside the retention window, newest destroyed first.
    dated_record_rows: list[tuple[datetime, DestroyedWorkspaceRow]] = []
    for user_id, account_email in accounts.items():
        for record in record_store.list_records(user_id):
            if record.state != RECORD_STATE_DESTROYED:
                continue
            destroyed_at = parse_destroyed_at(record.destroyed_at)
            has_env = read_canonical_env(paths, AgentId(record.agent_id)) is not None
            has_secrets = record.encrypted_secrets is not None
            dated_record_rows.append(
                (
                    destroyed_at if destroyed_at is not None else now,
                    DestroyedWorkspaceRow(
                        agent_id=record.agent_id,
                        display_name=record.display_name or record.agent_id,
                        account_label=account_email,
                        destroyed_at_display=destroyed_at.strftime("%Y-%m-%d") if destroyed_at is not None else "",
                        days_left_display=_days_left_display(destroyed_at, retention_seconds, now),
                        has_backup=has_env or has_secrets,
                        can_download=has_env,
                        is_locked=(not has_env) and has_secrets,
                        can_delete=True,
                        delete_hint="",
                    ),
                )
            )
    dated_record_rows.sort(key=lambda pair: pair[0], reverse=True)

    # Orphan envs this device holds with no record at all, newest file first.
    email_by_prefix = emails_by_bucket_owner_prefix(accounts)
    orphan_rows: list[DestroyedWorkspaceRow] = []
    for agent_id in reversed(list_orphan_env_agent_ids(paths, record_store)):
        env_content = read_canonical_env(paths, agent_id)
        owner_prefix = bucket_owner_prefix_from_env(env_content) if env_content is not None else None
        owner_email = email_by_prefix.get(owner_prefix) if owner_prefix is not None else None
        is_owner_signed_in = owner_email is not None or owner_prefix is None
        orphan_rows.append(
            DestroyedWorkspaceRow(
                agent_id=str(agent_id),
                display_name=f"unknown workspace ({agent_id})",
                account_label="this device",
                destroyed_at_display="",
                # Only imbue_cloud (R2) orphans are ever cleaned automatically;
                # BYO envs stay until the user deletes them here.
                days_left_display=(
                    "scheduled for cleanup" if owner_prefix is not None else "kept until you delete it"
                ),
                has_backup=True,
                can_download=True,
                is_locked=False,
                can_delete=is_owner_signed_in,
                delete_hint="" if is_owner_signed_in else "Sign in as the owning account to delete.",
            )
        )
    return [row for _, row in dated_record_rows] + orphan_rows


def _iter_workspace_records(session_store: MultiAccountSessionStore | None) -> Iterator[ReplicaRecord]:
    """Lazily yield every signed-in account's workspace records.

    A generator so the coordinate resolver only lists records on a discovery
    miss (listing can be slow).
    """
    record_store = session_store.record_store if session_store is not None else None
    if session_store is None or record_store is None:
        return
    for account in session_store.list_accounts():
        yield from record_store.list_records(str(account.user_id))


def _resolve_workspace_coordinate_to_agent_id(
    workspace_id: str,
    backend_resolver: BackendResolverInterface,
    workspace_records: Iterable[ReplicaRecord],
) -> AgentId | None:
    """Map an agent- or host-keyed coordinate to the stable agent id.

    Content URLs and restored windows are host-keyed; minds records are
    agent-keyed. Falls back to ``workspace_records`` (pass a lazy iterable so
    records are only listed on a miss) so a stopped host that discovery no
    longer reports still resolves.
    """
    if workspace_id.startswith("agent-"):
        try:
            return AgentId(workspace_id)
        except ValueError:
            return None
    if not workspace_id.startswith("host-"):
        return None
    for agent_id in backend_resolver.list_active_workspace_ids():
        display_info = backend_resolver.get_agent_display_info(agent_id)
        if display_info is not None and str(display_info.host_id) == workspace_id:
            return agent_id
    for record in workspace_records:
        if record.host_id == workspace_id and record.agent_id:
            return AgentId(record.agent_id)
    return None


def _resolve_workspace_coordinate(workspace_id: str) -> AgentId | None:
    """The request-scoped wrapper: resolve against the app state's resolver and records."""
    state = get_state()
    return _resolve_workspace_coordinate_to_agent_id(
        workspace_id, state.backend_resolver, _iter_workspace_records(state.session_store)
    )


def _build_ssh_command(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> str:
    ssh_info = backend_resolver.get_ssh_info(agent_id)
    if ssh_info is None:
        return ""
    return f"ssh -i {ssh_info.key_path} -p {ssh_info.port} {ssh_info.user}@{ssh_info.host}"


def _is_host_offline(backend_resolver: BackendResolverInterface, agent_id: AgentId) -> bool:
    display_info = backend_resolver.get_agent_display_info(agent_id)
    if display_info is None:
        return False
    # STOPPING counts: a mid-stop host is expectedly unreachable, and the
    # recovery restart (which waits for stopped before starting) is its remedy.
    return read_host_state(backend_resolver, display_info) in (
        HostState.STOPPED,
        HostState.STOPPING,
        HostState.CRASHED,
    )


def _handle_destroyed_workspaces() -> Response:
    if not _is_lifecycle_request_authenticated():
        return _error_response("Not authenticated", 401)
    response = DestroyedWorkspacesResponse(retention_days=_retention_days(), rows=tuple(_collect_destroyed_rows()))
    return _json_response(response, 200)


def _handle_delete_destroyed_backup(agent_id: str) -> Response:
    if not _is_lifecycle_request_authenticated():
        return _error_response("Not authenticated", 401)
    reaper = _get_backup_reaper()
    if reaper is None:
        return _error_response("Backup management is not configured on this install.", 409)
    state = get_state()
    session_store = state.session_store
    record_store = session_store.record_store if session_store is not None else None
    accounts = _signed_in_accounts_by_user_id()

    # Find the owning tombstoned record (if any); orphans have none.
    owning_user_id = ""
    owning_email = ""
    owning_record = None
    if record_store is not None:
        for user_id, account_email in accounts.items():
            for record in record_store.list_records(user_id):
                if record.state == RECORD_STATE_DESTROYED and record.agent_id == agent_id:
                    owning_user_id = user_id
                    owning_email = account_email
                    owning_record = record
                    break
            if owning_record is not None:
                break

    if owning_record is not None:
        destroyed_at = parse_destroyed_at(owning_record.destroyed_at)
        candidate = ReapCandidate(
            user_id=owning_user_id,
            account_email=owning_email,
            agent_id=owning_record.agent_id,
            host_id=owning_record.host_id,
            display_name=owning_record.display_name or owning_record.agent_id,
            destroyed_at=destroyed_at if destroyed_at is not None else datetime.now(timezone.utc),
        )
        is_deleted = reaper.reap_candidate(candidate, reason="user_requested")
    else:
        try:
            parsed_id = AgentId(agent_id)
        except ValueError:
            return _error_response(f"No destroyed machine found for {agent_id}.", 404)
        # delete_orphan_backup_now treats a missing env as already-deleted
        # (idempotent from its perspective), so a completely unknown id must
        # be rejected here rather than reported as a successful deletion.
        if read_canonical_env(reaper.paths, parsed_id) is None:
            return _error_response(f"No destroyed machine found for {agent_id}.", 404)
        is_deleted = reaper.delete_orphan_backup_now(parsed_id, accounts)
    if not is_deleted:
        return _error_response("Could not delete the backup; see the logs and try again.", 502)
    return Response('{"is_deleted": true}', status=200, mimetype="application/json")


def _handle_recovery_info(workspace_id: str) -> Response:
    if not _is_lifecycle_request_authenticated():
        return _error_response("Not authenticated", 401)
    resolved_id = _resolve_workspace_coordinate(workspace_id)
    if resolved_id is None:
        return _error_response(f"Unknown workspace {workspace_id}", 404)
    state = get_state()
    backend_resolver = state.backend_resolver
    tracker = state.system_interface_health_tracker
    workspace_name = backend_resolver.get_workspace_name(resolved_id)
    if not workspace_name:
        display_info = backend_resolver.get_agent_display_info(resolved_id)
        workspace_name = display_info.agent_name if display_info is not None else str(resolved_id)
    # Read every poll, not just the first: a provider error can land (or clear)
    # while the card is open, and it outranks whatever the machine's own health
    # says -- no restart routed through an unreachable backend can help.
    backend_verdict = read_backend_unreachable_verdict(resolved_id, backend_resolver=backend_resolver, tracker=tracker)
    # Read on the same poll and for the same reason: a device-side failure can
    # start or clear while the card is open, and it outranks the machine's own
    # health because it explains it -- the machine reads STUCK because this
    # device cannot reach it, whatever the machine is doing.
    device_verdict = read_device_cannot_connect_verdict(resolved_id, tracker=tracker)
    response = RecoveryInfoResponse(
        agent_id=str(resolved_id),
        workspace_name=workspace_name or str(resolved_id),
        health=tracker.get_health(resolved_id) if tracker is not None else AgentHealth.HEALTHY,
        health_error=(tracker.get_last_restart_error(resolved_id) or "") if tracker is not None else "",
        is_restart_start_only=tracker.get_restart_is_start_only(resolved_id) if tracker is not None else None,
        ssh_command=_build_ssh_command(backend_resolver, resolved_id),
        is_host_offline=_is_host_offline(backend_resolver, resolved_id),
        device_environment=read_environment_block(state.connectivity_detector, backend_resolver, resolved_id),
        is_backend_unreachable=backend_verdict is not None,
        provider_label=backend_verdict.provider_label if backend_verdict is not None else "",
        unreachable_reason=backend_verdict.reason if backend_verdict is not None else "",
        is_device_cannot_connect=device_verdict is not None,
        device_error_detail=device_verdict.detail if device_verdict is not None else "",
    )
    return _json_response(response, 200)


def register_lifecycle_routes(blueprint: Blueprint) -> None:
    """Register this area's /ui/api routes on the shared /ui blueprint."""
    blueprint.add_url_rule("/api/destroyed-workspaces", view_func=_handle_destroyed_workspaces)
    blueprint.add_url_rule(
        "/api/destroyed-workspaces/<agent_id>/delete-backup",
        view_func=_handle_delete_destroyed_backup,
        methods=["POST"],
    )
    blueprint.add_url_rule("/api/workspaces/<workspace_id>/recovery-info", view_func=_handle_recovery_info)
