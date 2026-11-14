"""The one idempotent "Update backup service" operation for a workspace.

Converges everything the verification check can find: the backup-service code
(checked out at the target ``minds-v*`` tag and committed as
``backup-update: <tag>``), the injected ``restic.env`` (re-injected from the
canonical copy, rotating a drifted workspace copy aside), and the supervisord
service (restarted and verified RUNNING by the apply script).

Runs as a tracked workspace operation (the ``workspace_operations`` registry,
like restart) so the settings view can poll step-level progress:

1. Gate + wait: poll the workspace's gate probe until no backup tick is in
   flight. RUNNING chat agents, UNKNOWN ones (state unreadable), and idle ones
   busy with a pending background task, block the *code* path -- the caller
   passes ``is_stop_chats`` (the "Stop all chats and retry" flow) to have the
   apply script stop them first. Waiting is unbounded and cancellable (the
   cancel just stops polling; nothing has been mutated yet).
2. Apply: one exec runs the mutating script (stash / checkout tag / commit /
   ``uv sync --all-packages`` / restart / verify), which auto-rolls-back via
   ``git revert``
   on failure. A stash-pop conflict is reported as a warning, never a failure.
3. Env: re-inject the canonical ``restic.env`` when one exists.
4. Verify: re-run the check; remaining code/env/service problems fail the
   operation, while NOT_CONFIGURED (fixed by the configure flow, not by
   update) merely logs.

A block on running chats is reported through the operation error string with
the ``BLOCKED_BY_RUNNING_CHATS:`` prefix so the UI can offer the
"Stop all chats and retry" action.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds.build_info import resolve_release_id
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client import backup_status
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.agent_address import build_agent_address
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backup_env_store import has_canonical_env
from imbue.minds.desktop_client.backup_env_store import read_archived_env
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import RESTIC_PREVIOUS_ENV_REMOTE_PATH
from imbue.minds.desktop_client.backup_provisioning import change_backup_destination_for_host
from imbue.minds.desktop_client.backup_provisioning import configure_backups_for_host
from imbue.minds.desktop_client.backup_provisioning import disable_backups_for_host
from imbue.minds.desktop_client.backup_provisioning import inject_previous_env_into_workspace
from imbue.minds.desktop_client.backup_provisioning import reinject_canonical_env
from imbue.minds.desktop_client.backup_provisioning import run_mngr_exec_on_agent
from imbue.minds.desktop_client.backup_verification import BackupServiceCheckState
from imbue.minds.desktop_client.backup_verification import BackupServiceProblem
from imbue.minds.desktop_client.backup_verification import check_backup_service_for_workspace
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudQuotaExceededCliError
from imbue.minds.desktop_client.workspace_facts_cache import WorkspaceFactsCache
from imbue.minds.desktop_client.workspace_operations import OperationFailureReason
from imbue.minds.desktop_client.workspace_operations import OperationOrProgramFailureReason
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationRegistryInterface
from imbue.minds.desktop_client.workspace_program_command import build_workspace_program_command
from imbue.minds.desktop_client.workspace_program_command import extract_marker_json
from imbue.minds.desktop_client.workspace_update_state import OLDEST_IN_PLACE_UPDATABLE_VERSION
from imbue.minds.desktop_client.workspace_update_state import is_below_in_place_update_floor
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.errors import ResticPasswordNotAllowedError
from imbue.minds.errors import UnrestorableSnapshotError
from imbue.minds.errors import WorkspaceFileWriteError
from imbue.minds.workspace_programs import backup_apply_update
from imbue.minds.workspace_programs import backup_gate_probe
from imbue.minds.workspace_programs import backup_restore
from imbue.minds.workspace_programs.backup_protocol import BackupProgramStatus
from imbue.minds.workspace_programs.backup_protocol import GATE_RESULT_MARKER
from imbue.minds.workspace_programs.backup_protocol import RESTORE_RESULT_MARKER
from imbue.minds.workspace_programs.backup_protocol import UPDATE_RESULT_MARKER
from imbue.minds.workspace_programs.backup_protocol import parse_backup_program_failure_reason
from imbue.minds.workspace_programs.backup_protocol import parse_backup_program_status
from imbue.minds.workspace_programs.backup_restore import RESUME_SERVICES_COMMAND
from imbue.mngr.primitives import AgentId

# Machine-readable prefix on the operation error when running chats block the
# update; the UI parses the comma-separated chat names after it.
BLOCKED_BY_RUNNING_CHATS_PREFIX: Final[str] = "BLOCKED_BY_RUNNING_CHATS:"

# Why the update refuses a workspace older than the in-place floor. The apply
# script checks the running release's `system/services/host_backup` out onto the
# workspace, and that code reads the backup root out of `<snapshot>/home` -- a
# layout only workspaces at or above the floor have. Installing it on an older
# one leaves a backup service that fails every tick from then on, which is
# strictly worse than the outdated service it replaced.
BELOW_UPDATE_FLOOR_MESSAGE: Final[str] = (
    "This workspace was created before "
    f"{OLDEST_IN_PLACE_UPDATABLE_VERSION}, and today's backup service does not work on it. "
    "Updating it would stop its backups altogether. Create a new workspace and move your work across."
)

# User-facing guidance when the apply script's `git stash pop` conflicted;
# shown on both the success (warning log) and failure (error message) paths so
# stashed changes never look lost.
_STASH_CONFLICT_GUIDANCE: Final[str] = (
    "Your uncommitted changes could not be restored automatically; "
    "they are preserved in the git stash (run `git stash pop` in the workspace)."
)

_CONVERGE_AFTER_RESTORE_FAILED_WARNING: Final[str] = (
    "The restore succeeded, but Imbue Studio couldn't finish setting up this workspace's backups afterwards. "
    'Run "Update backup software" to finish.'
)

# Must exceed the gate probe script's own `uv run --no-sync mngr list` budget (180s)
# so a slow list surfaces as the script's structured "mngr list failed"
# payload rather than an opaque exec timeout.
_GATE_PROBE_TIMEOUT_SECONDS: Final[float] = 240.0
_GATE_POLL_INTERVAL_SECONDS: Final[float] = 10.0
# The apply script bounds its own internal waits; this outer ceiling covers
# the worst-case rollback path (in-script tick wait of 900s, two `uv sync`
# runs of up to 900s each, plus restarts/verifies and the revert) so the
# script's structured failure payload always beats the exec timeout.
_APPLY_EXEC_TIMEOUT_SECONDS: Final[float] = 3600.0
# The restore script's dominant costs are two restic passes (safety snapshot +
# restore) plus its tick wait and a `uv sync`; the ceiling must exceed their
# sum so the script's structured failure payload always beats the exec timeout.
_RESTORE_EXEC_TIMEOUT_SECONDS: Final[float] = 9000.0
# Ceiling for the best-effort RESUME_SERVICES_COMMAND dispatched when the
# restore exec dies without reporting; restarting every service can take a
# couple of minutes on a loaded workspace.
_SERVICE_RESUME_TIMEOUT_SECONDS: Final[float] = 360.0
# Listing snapshots to resolve the restore target reads the whole repository
# index, so it gets a longer ceiling than the status route's snappy default
# (nothing is rendering behind this -- the operation is already dispatched).
_SNAPSHOT_RESOLVE_TIMEOUT_SECONDS: Final[float] = 120.0

# Problems that mean the update itself did not converge (operation fails).
# NOT_CONFIGURED is deliberately absent: enabling backups is the configure
# flow's job, and an update on an unconfigured workspace still usefully
# converges the code + service.
_PROBLEMS_THAT_FAIL_UPDATE: Final[tuple[BackupServiceProblem, ...]] = (
    BackupServiceProblem.CODE_OUTDATED,
    BackupServiceProblem.ENV_MISSING,
    BackupServiceProblem.ENV_MISMATCH,
    BackupServiceProblem.SERVICE_NOT_RUNNING,
    BackupServiceProblem.UNVERIFIABLE,
)


class RestoreCaveat(FrozenModel):
    """What a restore that succeeded left undone, told as its completion warning."""

    warning: str = Field(description="The completion warning the user reads")
    is_update_suggested: bool = Field(description='Whether running "Update backup software" finishes it')


class BackupOperationFailure(FrozenModel):
    """Why a backup step failed: the raw detail, and the known reason when there is one."""

    detail: str = Field(description="The failure text, kept for the details and the logs")
    reason: OperationOrProgramFailureReason | None = Field(description="Why it failed, when the code knows; else None")


class BackupWorkerFailureHandler(MutableModel):
    """Callable ``on_failure`` hook for backup update/configure worker threads.

    If the worker thread crashes unexpectedly, the ``ConcurrencyGroup`` invokes
    this so the operation registry still reaches FAILED instead of the settings
    view polling forever. The crash itself is logged by the observable-thread
    machinery; this only records the operation state.
    """

    workspace_agent_id: AgentId = Field(frozen=True, description="Workspace whose backup worker crashed.")
    registry: WorkspaceOperationRegistryInterface = Field(
        frozen=True, description="In-memory operation registry to mark FAILED."
    )

    def __call__(self, exc: BaseException) -> None:
        self.registry.fail(self.workspace_agent_id, f"The backup worker failed unexpectedly: {exc}")


def run_backup_update_sequence(
    *,
    agent_id: AgentId,
    paths: InstallationPaths,
    resolver: BackendResolverInterface,
    facts_cache: WorkspaceFactsCache,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    is_stop_chats: bool,
    workspace_version_ref: str | None,
) -> None:
    """Worker-thread entry point: run the whole update operation for one workspace.

    The caller has already registered the operation (``registry.start``); this
    function ends it via ``registry.complete`` / ``registry.fail``.
    """
    try:
        _run_update_phases(
            agent_id=agent_id,
            paths=paths,
            resolver=resolver,
            facts_cache=facts_cache,
            registry=registry,
            parent_cg=parent_cg,
            is_stop_chats=is_stop_chats,
            workspace_version_ref=workspace_version_ref,
        )
    except BackupProvisioningError as exc:
        logger.warning("Backup update for {} failed: {}", agent_id, exc)
        registry.fail(agent_id, str(exc))


def _run_update_phases(
    *,
    agent_id: AgentId,
    paths: InstallationPaths,
    resolver: BackendResolverInterface,
    facts_cache: WorkspaceFactsCache,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    is_stop_chats: bool,
    workspace_version_ref: str | None,
) -> None:
    below_floor_refusal = _refuse_update_below_floor(agent_id, workspace_version_ref)
    if below_floor_refusal is not None:
        registry.fail_with_reason(agent_id, below_floor_refusal, OperationFailureReason.BELOW_UPDATE_FLOOR)
        return

    # Phase 1: gate + wait (cancellable; nothing has been mutated yet).
    registry.append_log(agent_id, "Checking for running chats and in-progress backups...")
    if not _wait_for_quiet_workspace(
        agent_id=agent_id,
        agent_address=build_agent_address(agent_id, resolver),
        registry=registry,
        parent_cg=parent_cg,
        is_stop_chats=is_stop_chats,
    ):
        return

    # Point of no return: claim it atomically so a cancel that raced the
    # gate's last poll is honored here instead of being silently ignored for
    # the whole mutating exec. From this point the status API reports the
    # operation as no longer cancellable.
    if not registry.begin_mutation(agent_id):
        registry.cancel(agent_id)
        return

    # Phases 2-4: apply, re-inject the env, verify (shared with the restore's
    # chained update).
    update_error = _apply_update_and_verify(
        agent_id=agent_id,
        paths=paths,
        resolver=resolver,
        facts_cache=facts_cache,
        registry=registry,
        parent_cg=parent_cg,
        is_stop_chats=is_stop_chats,
        workspace_version_ref=workspace_version_ref,
    )
    if update_error is not None:
        registry.fail_with_reason(agent_id, update_error.detail, update_error.reason)
        return
    registry.complete(agent_id)


class _ExecLogForwarder(MutableModel):
    """``on_output`` callback streaming a workspace exec's output into the operation log.

    The scripts' marker verdict line is excluded -- it is machine-readable
    plumbing, parsed separately -- and blank lines are dropped. Everything
    else (the restore script's phase/progress lines, stray warnings, crash
    tracebacks on stderr) lands in the log so the user can follow along live.
    """

    workspace_agent_id: AgentId = Field(frozen=True, description="Workspace whose operation log receives the lines.")
    registry: WorkspaceOperationRegistryInterface = Field(
        frozen=True, description="In-memory operation registry holding the log."
    )

    def __call__(self, line: str, is_stdout: bool) -> None:
        stripped = line.rstrip()
        if not stripped.strip():
            return
        if stripped.startswith(RESTORE_RESULT_MARKER) or stripped.startswith(UPDATE_RESULT_MARKER):
            return
        self.registry.append_log(self.workspace_agent_id, stripped)


def _refuse_update_below_floor(agent_id: AgentId, workspace_version_ref: str | None) -> str | None:
    """The refusal message when this workspace predates the in-place update floor, else None."""
    if not is_below_in_place_update_floor(workspace_version_ref):
        return None
    logger.warning(
        "Refused the backup-service update for {}: version {} predates {}",
        agent_id,
        workspace_version_ref,
        OLDEST_IN_PLACE_UPDATABLE_VERSION,
    )
    return BELOW_UPDATE_FLOOR_MESSAGE


def _apply_update_and_verify(
    *,
    agent_id: AgentId,
    paths: InstallationPaths,
    resolver: BackendResolverInterface,
    facts_cache: WorkspaceFactsCache,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    is_stop_chats: bool,
    # The workspace's own template version, as the update detector resolved it.
    # None when it has none.
    workspace_version_ref: str | None,
) -> BackupOperationFailure | None:
    """Run the mutating update script, re-inject the env, and verify convergence.

    Returns the failure (its detail possibly ``BLOCKED_BY_RUNNING_CHATS_PREFIX``-
    prefixed), or None on success. Shared by the standalone update operation
    (which fails the operation on error) and the restore's chained update
    (which downgrades an error to a completion warning).
    """
    below_floor_refusal = _refuse_update_below_floor(agent_id, workspace_version_ref)
    if below_floor_refusal is not None:
        return BackupOperationFailure(detail=below_floor_refusal, reason=OperationFailureReason.BELOW_UPDATE_FLOOR)

    # The mutating apply script (stash/checkout/commit/sync/restart).
    registry.append_log(agent_id, "Applying the backup service update...")
    apply_command = build_workspace_program_command(
        backup_apply_update,
        (
            "--minds-version",
            resolve_release_id(),
            "--agent-id",
            str(agent_id),
        )
        + (("--stop-chats",) if is_stop_chats else ()),
    )
    agent_address = build_agent_address(agent_id, resolver)
    apply_result = run_mngr_exec_on_agent(
        agent_address,
        apply_command,
        parent_cg=parent_cg,
        timeout_seconds=_APPLY_EXEC_TIMEOUT_SECONDS,
        on_output=_ExecLogForwarder(workspace_agent_id=agent_id, registry=registry),
    )
    payload = extract_marker_json(apply_result.stdout, UPDATE_RESULT_MARKER)
    if payload is None:
        detail = (apply_result.stderr or apply_result.stdout).strip()[-800:]
        return BackupOperationFailure(
            detail=f"The update script produced no result: {detail}", reason=OperationFailureReason.NO_RESULT
        )
    match parse_backup_program_status(payload.get("status")):
        case BackupProgramStatus.BLOCKED:
            running_chats = payload.get("running_chats")
            chat_names = ",".join(str(name) for name in running_chats) if isinstance(running_chats, list) else ""
            return BackupOperationFailure(detail=f"{BLOCKED_BY_RUNNING_CHATS_PREFIX}{chat_names}", reason=None)
        case BackupProgramStatus.FAILED:
            detail = str(payload.get("detail", "unknown failure"))
            rolled_back_note = " (changes were rolled back)" if payload.get("rolled_back") else ""
            is_stash_kept = bool(payload.get("stash_conflict"))
            stash_note = f" {_STASH_CONFLICT_GUIDANCE}" if is_stash_kept else ""
            script_reason = parse_backup_program_failure_reason(payload.get("failure_reason"))
            reason = (
                script_reason
                if script_reason is not None
                else (OperationFailureReason.UPDATE_STASH_KEPT if is_stash_kept else None)
            )
            return BackupOperationFailure(detail=f"{detail}{rolled_back_note}{stash_note}", reason=reason)
        case BackupProgramStatus.OK:
            pass
        case _ as unreachable:
            assert_never(unreachable)
    if payload.get("committed"):
        registry.append_log(agent_id, f"Updated backup service code to {payload.get('tag', 'the target tag')}.")
    else:
        registry.append_log(agent_id, "Backup service code already matched the target version.")
    if payload.get("stash_conflict"):
        registry.append_log(agent_id, f"Warning: {_STASH_CONFLICT_GUIDANCE}")

    # Re-inject the canonical env (rotates a drifted workspace copy).
    if has_canonical_env(paths, agent_id):
        registry.append_log(agent_id, "Re-injecting backup credentials...")
        reinject_canonical_env(agent_id=agent_id, agent_address=agent_address, paths=paths, parent_cg=parent_cg)

    # Verify convergence with a fresh check.
    registry.append_log(agent_id, "Verifying the backup service...")
    check = check_backup_service_for_workspace(
        paths, agent_id, resolver=resolver, facts_cache=facts_cache, parent_cg=parent_cg
    )
    failing = tuple(problem for problem in check.problems if problem in _PROBLEMS_THAT_FAIL_UPDATE)
    if check.state == BackupServiceCheckState.PROBLEMS and failing:
        problem_names = ", ".join(problem.value for problem in failing)
        return BackupOperationFailure(
            detail=f"The update ran but verification still reports: {problem_names}. {check.detail}", reason=None
        )
    if BackupServiceProblem.NOT_CONFIGURED in check.problems:
        registry.append_log(
            agent_id, "Backups are still not configured for this workspace; enable them from the backup settings."
        )
    return None


def _wait_for_quiet_workspace(
    *,
    agent_id: AgentId,
    agent_address: str,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    is_stop_chats: bool,
) -> bool:
    """Poll the gate probe until no backup tick is in flight; returns False when the op ended.

    Chats found while ``is_stop_chats`` is False end the operation with the
    structured blocked error immediately (no point waiting out a backup tick
    first). Cancellation between polls ends the operation as failed
    ("cancelled"); nothing has been mutated at this point.
    """
    probe_command = build_workspace_program_command(backup_gate_probe, ("--agent-id", str(agent_id)))
    is_waiting_logged = False
    is_gate_error_logged = False
    while not registry.is_cancel_requested(agent_id):
        probe_result = run_mngr_exec_on_agent(
            agent_address, probe_command, parent_cg=parent_cg, timeout_seconds=_GATE_PROBE_TIMEOUT_SECONDS
        )
        payload = extract_marker_json(probe_result.stdout, GATE_RESULT_MARKER)
        if payload is None:
            detail = (probe_result.stderr or probe_result.stdout).strip()[-500:]
            registry.fail_with_reason(
                agent_id, f"Could not probe the workspace: {detail}", OperationFailureReason.WORKSPACE_PROBE_FAILED
            )
            return False
        # A gate_error means the probe could not list chats (its mngr list
        # failed) and running_chats is empty by construction. Keep going --
        # the apply script re-runs the gate authoritatively before mutating
        # anything -- but leave a trace so a later failure is explicable.
        gate_error = str(payload.get("gate_error") or "")
        if gate_error and not is_gate_error_logged:
            logger.warning("Backup update gate probe for {} could not list chats: {}", agent_id, gate_error)
            registry.append_log(
                agent_id, "Warning: could not check for running chats; they are re-checked before applying."
            )
            is_gate_error_logged = True
        running_chats = payload.get("running_chats")
        chat_names = [str(name) for name in running_chats] if isinstance(running_chats, list) else []
        if chat_names and not is_stop_chats:
            registry.fail(agent_id, f"{BLOCKED_BY_RUNNING_CHATS_PREFIX}{','.join(chat_names)}")
            return False
        if not payload.get("backup_tick_in_flight"):
            return True
        if not is_waiting_logged:
            registry.append_log(agent_id, "Waiting for the in-progress backup to finish...")
            is_waiting_logged = True
        # Wakes immediately on a cancel request instead of sleeping it out.
        registry.wait_for_cancel(agent_id, _GATE_POLL_INTERVAL_SECONDS)
    registry.cancel(agent_id)
    return False


def run_backup_restore_sequence(
    *,
    agent_id: AgentId,
    paths: InstallationPaths,
    resolver: BackendResolverInterface,
    facts_cache: WorkspaceFactsCache,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    snapshot_id: str,
    is_stop_chats: bool,
    is_update_after: bool,
    is_skip_safety_snapshot: bool,
    is_skip_chat_gate: bool,
    workspace_version_ref: str | None,
    # None restores from the workspace's current backups; an archive id from
    # one of its previous backup destinations (an archived canonical env).
    archive_id: str | None = None,
) -> None:
    """Worker-thread entry point: restore one workspace to one restic snapshot, in place.

    The caller has already registered the operation (``registry.start``); this
    function ends it via ``registry.complete`` / ``registry.fail`` /
    ``registry.cancel``.
    """
    try:
        _run_restore_phases(
            agent_id=agent_id,
            paths=paths,
            resolver=resolver,
            facts_cache=facts_cache,
            registry=registry,
            parent_cg=parent_cg,
            snapshot_id=snapshot_id,
            archive_id=archive_id,
            is_stop_chats=is_stop_chats,
            is_update_after=is_update_after,
            is_skip_safety_snapshot=is_skip_safety_snapshot,
            is_skip_chat_gate=is_skip_chat_gate,
            workspace_version_ref=workspace_version_ref,
        )
    except BackupProvisioningError as exc:
        logger.warning("Backup restore for {} failed: {}", agent_id, exc)
        registry.fail(agent_id, str(exc))


def _resolve_restore_snapshot(
    *,
    agent_id: AgentId,
    paths: InstallationPaths,
    snapshot_id: str,
    archive_id: str | None,
    parent_cg: ConcurrencyGroup | None,
) -> restic_cli.ResticSnapshot:
    """Resolve the snapshot to restore, from Imbue Studio's own view of the repository.

    Imbue Studio holds the canonical restic.env, so it can read the snapshot's
    recorded root and timestamp here -- the in-workspace script does not need
    to re-query restic for metadata Imbue Studio already has. Doing it before the
    workspace is touched also means a bad snapshot id fails the operation
    outright, rather than after the services are stopped and a safety snapshot
    has been taken for nothing.
    """
    snapshots = backup_status.list_workspace_snapshots(
        paths, agent_id, parent_cg=parent_cg, timeout_seconds=_SNAPSHOT_RESOLVE_TIMEOUT_SECONDS, archive_id=archive_id
    )
    for snapshot in snapshots:
        if snapshot_id in (snapshot.snapshot_id, snapshot.short_id):
            if not snapshot.paths:
                raise UnrestorableSnapshotError(f"Snapshot {snapshot_id} records no paths; it cannot be restored")
            return snapshot
    raise UnrestorableSnapshotError(f"No backup {snapshot_id} exists for this workspace")


def _resolve_restore_subpath(
    *,
    agent_id: AgentId,
    paths: InstallationPaths,
    snapshot: restic_cli.ResticSnapshot,
    archive_id: str | None,
    parent_cg: ConcurrencyGroup | None,
) -> str:
    """Locate the workspace subtree inside the snapshot (identified by its repo checkout).

    The current layout backs up the unified ``/home/user`` tree, whose repo
    checkout is a ``workspace/`` child; legacy snapshots (pre-layout-move)
    carry a ``code/`` checkout instead, either at the root (plain docker) or
    one level down in a ``host_dir/`` child (btrfs providers, which
    snapshotted the whole unified host volume). All of those sit under the
    snapshot's recorded path, except in snapshots host_backup takes by backing
    up ``.`` from inside the tree: those hold ``workspace/`` at the snapshot's
    own root, so the subpath is ``/``. Resolved here, from Imbue Studio's own view of
    the repository, so the in-workspace script only ever consumes a validated
    ``<snapshot>:<subpath>`` -- restoring the wrong level would wreck the
    workspace.
    """
    root = snapshot.paths[0]
    root_entries = backup_status.list_workspace_snapshot_directory(
        paths,
        agent_id,
        snapshot_id=snapshot.snapshot_id,
        directory=root,
        parent_cg=parent_cg,
        timeout_seconds=_SNAPSHOT_RESOLVE_TIMEOUT_SECONDS,
        archive_id=archive_id,
    )
    if f"{root}/workspace" in root_entries or f"{root}/code" in root_entries:
        return root
    nested_root = f"{root}/host_dir"
    if nested_root in root_entries:
        nested_entries = backup_status.list_workspace_snapshot_directory(
            paths,
            agent_id,
            snapshot_id=snapshot.snapshot_id,
            directory=nested_root,
            parent_cg=parent_cg,
            timeout_seconds=_SNAPSHOT_RESOLVE_TIMEOUT_SECONDS,
            archive_id=archive_id,
        )
        if f"{nested_root}/workspace" in nested_entries or f"{nested_root}/code" in nested_entries:
            return nested_root
    tree_root_entries = backup_status.list_workspace_snapshot_directory(
        paths,
        agent_id,
        snapshot_id=snapshot.snapshot_id,
        directory="/",
        parent_cg=parent_cg,
        timeout_seconds=_SNAPSHOT_RESOLVE_TIMEOUT_SECONDS,
        archive_id=archive_id,
    )
    if "/workspace" in tree_root_entries:
        return "/"
    raise UnrestorableSnapshotError(
        f"Snapshot {snapshot.short_id} does not contain a workspace (no workspace/ or code/ checkout); "
        "it cannot be restored"
    )


def _restore_services_down(payload: dict[str, object]) -> tuple[str, ...]:
    """The restore script's ``services_down`` list: programs not yet running again.

    These services are outside the restore-critical set (the contract is
    ``behaviors/backup-restore/restore-verdict.feature``), so their state
    never fails the operation -- the workspace came back able to serve its
    user. They ride on the record as data, and the UI words the caveat.
    """
    services_down = payload.get("services_down")
    if not isinstance(services_down, list):
        return ()
    return tuple(str(name) for name in services_down)


def _restore_service_config_error(payload: dict[str, object]) -> str | None:
    """The restore program's ``supervisord_config_error``: supervisorctl's output when the restored config did not parse.

    The program then brought the services back from the configuration
    supervisord loaded before the restore, and a supervisord that starts from
    the restored one will refuse to. It rides on the record as data, and the
    UI words the consequence.
    """
    config_error = payload.get("supervisord_config_error")
    if not isinstance(config_error, str) or not config_error:
        return None
    return config_error


def _end_unfinished_restore(
    registry: WorkspaceOperationRegistryInterface, agent_id: AgentId, payload: dict[str, object]
) -> bool:
    """End the operation when the restore program reports it blocked or failed; return whether it did."""
    match parse_backup_program_status(payload.get("status")):
        case BackupProgramStatus.BLOCKED:
            running_chats = payload.get("running_chats")
            chat_names = ",".join(str(name) for name in running_chats) if isinstance(running_chats, list) else ""
            registry.fail(agent_id, f"{BLOCKED_BY_RUNNING_CHATS_PREFIX}{chat_names}")
            return True
        case BackupProgramStatus.FAILED:
            detail = str(payload.get("detail", "unknown failure"))
            is_safety_backup_taken = bool(payload.get("safety_snapshot_taken"))
            safety_note = (
                " A safety backup of the pre-restore state was taken first." if is_safety_backup_taken else ""
            )
            script_reason = parse_backup_program_failure_reason(payload.get("failure_reason"))
            reason = (
                script_reason
                if script_reason is not None
                else (
                    OperationFailureReason.RESTORE_INCOMPLETE_AFTER_SAFETY_BACKUP
                    if is_safety_backup_taken
                    else OperationFailureReason.RESTORE_INCOMPLETE
                )
            )
            registry.fail_restore(agent_id, f"{detail}{safety_note}", reason, _restore_service_config_error(payload))
            return True
        case BackupProgramStatus.OK:
            return False
        case _ as unreachable:
            assert_never(unreachable)


def _complete_restore(
    registry: WorkspaceOperationRegistryInterface,
    agent_id: AgentId,
    caveat: RestoreCaveat | None,
    payload: dict[str, object],
) -> None:
    registry.complete_restore(
        agent_id,
        caveat.warning if caveat is not None else None,
        caveat is not None and caveat.is_update_suggested,
        _restore_services_down(payload),
        _restore_service_config_error(payload),
    )


def _chained_update_warning(update_error: BackupOperationFailure) -> RestoreCaveat:
    """Word a chained-update failure as a completion warning (the restore itself succeeded)."""
    detail = update_error.detail
    if detail.startswith(BLOCKED_BY_RUNNING_CHATS_PREFIX):
        names = detail[len(BLOCKED_BY_RUNNING_CHATS_PREFIX) :]
        names_note = f" ({names})" if names else ""
        return RestoreCaveat(
            warning=(
                f"The restore succeeded, but the backup service update afterwards was blocked by running "
                f'chats{names_note}. Run "Update backup software" once they are stopped.'
            ),
            is_update_suggested=True,
        )
    if update_error.reason is OperationFailureReason.BELOW_UPDATE_FLOOR:
        # A refusal, not a failure: it will be refused every time, Settings hides
        # the button for this machine, and the message already says what to do.
        return RestoreCaveat(
            warning=f"The restore succeeded, but the backup service was left as it is. {BELOW_UPDATE_FLOOR_MESSAGE}",
            is_update_suggested=False,
        )
    return RestoreCaveat(
        warning=(
            f"The restore succeeded, but updating the backup service afterwards failed: {detail} "
            'You can retry it with "Update backup software".'
        ),
        is_update_suggested=True,
    )


def _run_restore_phases(
    *,
    agent_id: AgentId,
    paths: InstallationPaths,
    resolver: BackendResolverInterface,
    facts_cache: WorkspaceFactsCache,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    snapshot_id: str,
    archive_id: str | None,
    is_stop_chats: bool,
    is_update_after: bool,
    is_skip_safety_snapshot: bool,
    is_skip_chat_gate: bool,
    workspace_version_ref: str | None,
) -> None:
    # Phase 0: resolve the snapshot and its host-dir subpath before anything
    # waits or mutates, so an unknown id (or a snapshot with no workspace in
    # it) fails fast and cheaply.
    try:
        snapshot = _resolve_restore_snapshot(
            agent_id=agent_id, paths=paths, snapshot_id=snapshot_id, archive_id=archive_id, parent_cg=parent_cg
        )
        snapshot_subpath = _resolve_restore_subpath(
            agent_id=agent_id, paths=paths, snapshot=snapshot, archive_id=archive_id, parent_cg=parent_cg
        )
    except UnrestorableSnapshotError as exc:
        logger.warning("Backup restore for {} refused the snapshot: {}", agent_id, exc)
        registry.fail_with_reason(agent_id, str(exc), OperationFailureReason.SNAPSHOT_UNRESTORABLE)
        return
    except BackupProvisioningError as exc:
        logger.warning("Backup restore for {} could not read the backup store: {}", agent_id, exc)
        registry.fail_with_reason(agent_id, str(exc), OperationFailureReason.SNAPSHOT_READ_FAILED)
        return

    # Phase 0.5: converge the workspace's restic.env to the canonical copy
    # before dispatch -- the restore script takes its safety and restored-state
    # snapshots with the workspace file's credentials, and precisely the
    # workspaces most in need of a restore (ENV_MISSING / ENV_MISMATCH) would
    # otherwise fail. A differing workspace copy is archived aside, never
    # destroyed. A restore from a previous destination hands that archived env
    # to the script separately (phase 2), so the workspace's own env, which
    # host_backup ticks against, is never pointed at the retired repository.
    agent_address = build_agent_address(agent_id, resolver)
    registry.append_log(agent_id, "Making sure the workspace has the right backup credentials...")
    try:
        reinject_canonical_env(agent_id=agent_id, agent_address=agent_address, paths=paths, parent_cg=parent_cg)
    except WorkspaceFileWriteError as exc:
        logger.warning("Backup restore for {} could not write the credentials: {}", agent_id, exc)
        registry.fail_with_reason(agent_id, str(exc), OperationFailureReason.CREDENTIALS_WRITE_FAILED)
        return
    source_env_content: str | None = None
    if archive_id is not None:
        source_env_content = read_archived_env(paths, agent_id, archive_id)
        if source_env_content is None:
            raise BackupProvisioningError(f"No previous backup storage {archive_id} exists for {agent_id}")
    _run_restore_from_workspace_env(
        agent_id=agent_id,
        agent_address=agent_address,
        paths=paths,
        resolver=resolver,
        facts_cache=facts_cache,
        registry=registry,
        parent_cg=parent_cg,
        snapshot_id=snapshot_id,
        snapshot=snapshot,
        snapshot_subpath=snapshot_subpath,
        source_env_content=source_env_content,
        is_stop_chats=is_stop_chats,
        is_update_after=is_update_after,
        is_skip_safety_snapshot=is_skip_safety_snapshot,
        is_skip_chat_gate=is_skip_chat_gate,
        workspace_version_ref=workspace_version_ref,
    )


@pure
def build_restore_program_arguments(
    *,
    agent_id: AgentId,
    snapshot_id: str,
    snapshot_subpath: str,
    snapshot_time: datetime,
    is_from_previous_storage: bool,
    is_stop_chats: bool,
    is_skip_chat_gate: bool,
    is_skip_safety_snapshot: bool,
) -> tuple[str, ...]:
    """The restore program's argv: the snapshot metadata the desktop resolved, plus the flags."""
    return (
        (
            "--agent-id",
            str(agent_id),
            "--snapshot-id",
            snapshot_id,
            # Resolved by the desktop from its own view of the repository, so the
            # script never re-queries restic for this metadata.
            "--snapshot-subpath",
            snapshot_subpath,
            "--source-time",
            snapshot_time.isoformat(),
        )
        + (("--source-env-path", RESTIC_PREVIOUS_ENV_REMOTE_PATH) if is_from_previous_storage else ())
        + (("--stop-chats",) if is_stop_chats else ())
        + (("--skip-chat-gate",) if is_skip_chat_gate else ())
        + (("--skip-safety-snapshot",) if is_skip_safety_snapshot else ())
    )


def _run_restore_from_workspace_env(
    *,
    agent_id: AgentId,
    agent_address: str,
    paths: InstallationPaths,
    resolver: BackendResolverInterface,
    facts_cache: WorkspaceFactsCache,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    snapshot_id: str,
    snapshot: restic_cli.ResticSnapshot,
    snapshot_subpath: str,
    # The archived env of a previous destination to restore from; None
    # restores from the workspace's current repository.
    source_env_content: str | None,
    is_stop_chats: bool,
    is_update_after: bool,
    is_skip_safety_snapshot: bool,
    is_skip_chat_gate: bool,
    workspace_version_ref: str | None,
) -> None:
    """Phases 1-4 of a restore, against the restic.env the workspace holds."""
    # Phase 1: gate + wait (cancellable; nothing has been mutated yet). Kept
    # even for a forced restore: the probe tolerates a broken `mngr list`
    # (gate_error) by design, and real knowledge of running chats must still
    # block -- the force flag only skips the check the workspace can no
    # longer answer.
    registry.append_log(agent_id, "Checking for running chats and in-progress backups...")
    if not _wait_for_quiet_workspace(
        agent_id=agent_id,
        agent_address=agent_address,
        registry=registry,
        parent_cg=parent_cg,
        is_stop_chats=is_stop_chats,
    ):
        return

    # Point of no return: claim it atomically so a cancel that raced the
    # gate's last poll is honored here instead of being silently ignored for
    # the whole (long) restore exec. From this point the status API reports
    # the operation as no longer cancellable. Known, accepted window: the
    # script's own authoritative gate may still wait out a tick that started
    # in the dispatch race (bounded by its TICK_WAIT_TIMEOUT) with Cancel
    # already withdrawn -- a dispatched exec cannot be stopped, and nothing
    # has mutated while it waits.
    if not registry.begin_mutation(agent_id):
        registry.cancel(agent_id)
        return

    # Phase 2: one exec runs the whole restore (safety snapshot / in-place
    # sync restore / env write-back / uv sync / restart services) and reports
    # a verdict, streaming its phase + restic progress lines into the
    # operation log as it runs. A previous destination's env is handed over
    # only now, past the point of no return, as a side file the script reads
    # and removes.
    if source_env_content is not None:
        registry.append_log(agent_id, "Handing the previous backup storage's credentials to the workspace...")
        inject_previous_env_into_workspace(agent_address, source_env_content, parent_cg=parent_cg)
    registry.append_log(
        agent_id, "Backing up the current state, then restoring the selected backup. This can take a while..."
    )
    restore_command = build_workspace_program_command(
        backup_restore,
        build_restore_program_arguments(
            agent_id=agent_id,
            snapshot_id=snapshot_id,
            snapshot_subpath=snapshot_subpath,
            snapshot_time=snapshot.time,
            is_from_previous_storage=source_env_content is not None,
            is_stop_chats=is_stop_chats,
            is_skip_chat_gate=is_skip_chat_gate,
            is_skip_safety_snapshot=is_skip_safety_snapshot,
        ),
    )
    restore_result = run_mngr_exec_on_agent(
        agent_address,
        restore_command,
        parent_cg=parent_cg,
        timeout_seconds=_RESTORE_EXEC_TIMEOUT_SECONDS,
        on_output=_ExecLogForwarder(workspace_agent_id=agent_id, registry=registry),
    )
    payload = extract_marker_json(restore_result.stdout, RESTORE_RESULT_MARKER)
    if payload is None:
        # The script resumes the workspace services itself on every failure it
        # can report; no payload means it died mid-run (e.g. the exec timeout
        # killed it) -- possibly after stopping every supervisord service.
        # Best-effort: bring them back before reporting the failure, so a
        # killed restore cannot leave backups (and the whole workspace) down.
        detail = (restore_result.stderr or restore_result.stdout).strip()[-800:]
        registry.append_log(agent_id, "The restore did not report a result; restarting the workspace services...")
        resume_result = run_mngr_exec_on_agent(
            agent_address,
            RESUME_SERVICES_COMMAND,
            parent_cg=parent_cg,
            timeout_seconds=_SERVICE_RESUME_TIMEOUT_SECONDS,
        )
        if resume_result.returncode != 0:
            resume_detail = (resume_result.stderr or resume_result.stdout).strip()[-300:]
            logger.warning("Post-failure service resume for {} failed: {}", agent_id, resume_detail)
        registry.fail_with_reason(
            agent_id, f"The restore script produced no result: {detail}", OperationFailureReason.NO_RESULT
        )
        return
    if _end_unfinished_restore(registry, agent_id, payload):
        return
    registry.append_log(
        agent_id, "Restored the backup, reinstalled dependencies, and restarted the workspace services."
    )
    caveat = _converge_after_restore(
        agent_id=agent_id,
        agent_address=agent_address,
        paths=paths,
        resolver=resolver,
        facts_cache=facts_cache,
        registry=registry,
        parent_cg=parent_cg,
        is_stop_chats=is_stop_chats,
        is_update_after=is_update_after,
        workspace_version_ref=workspace_version_ref,
    )
    _complete_restore(registry, agent_id, caveat, payload)


def _converge_after_restore(
    *,
    agent_id: AgentId,
    agent_address: str,
    paths: InstallationPaths,
    resolver: BackendResolverInterface,
    facts_cache: WorkspaceFactsCache,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    is_stop_chats: bool,
    is_update_after: bool,
    workspace_version_ref: str | None,
) -> RestoreCaveat | None:
    """Bring a restored workspace's backup credentials and code back to current; return what it left undone.

    The user's data is restored, which is what they asked for, so a failure
    to re-inject the credentials or to update the backup software downgrades
    to a warning rather than failing the operation.
    """
    try:
        # Phase 3: the script wrote back the pre-restore restic.env, but
        # re-inject the canonical copy anyway so the workspace ends converged
        # even if the env had drifted before the restore. Must precede phase 4,
        # whose verification compares the two.
        if has_canonical_env(paths, agent_id):
            registry.append_log(agent_id, "Re-injecting backup credentials...")
            reinject_canonical_env(agent_id=agent_id, agent_address=agent_address, paths=paths, parent_cg=parent_cg)

        # Phase 4 (default-on): the restored snapshot may carry arbitrarily old
        # backup-service code; the idempotent update brings it back to the
        # current version.
        if not is_update_after:
            return None
        registry.append_log(agent_id, "Updating the backup service to the current version...")
        update_error = _apply_update_and_verify(
            agent_id=agent_id,
            paths=paths,
            resolver=resolver,
            facts_cache=facts_cache,
            registry=registry,
            parent_cg=parent_cg,
            is_stop_chats=is_stop_chats,
            workspace_version_ref=workspace_version_ref,
        )
    except BackupProvisioningError as exc:
        logger.warning("Converging the backup service after the restore for {} failed: {}", agent_id, exc)
        return RestoreCaveat(warning=_CONVERGE_AFTER_RESTORE_FAILED_WARNING, is_update_suggested=True)
    if update_error is None:
        return None
    logger.warning("Chained backup-service update after restore for {} failed: {}", agent_id, update_error.detail)
    return _chained_update_warning(update_error)


def run_backup_configure_sequence(
    *,
    agent_id: AgentId,
    # How ``mngr`` reaches the workspace (see ``build_agent_address``)
    agent_address: str,
    request: BackupSetupRequest,
    imbue_cloud_cli: ImbueCloudCli | None,
    paths: InstallationPaths,
    parent_cg: ConcurrencyGroup | None,
    registry: WorkspaceOperationRegistryInterface,
    is_destination_change: bool,
    quota_evictor: Callable[[], bool] | None = None,
) -> None:
    """Worker-thread entry point for the enable / change-destination operation.

    Env-only (never touches the workspace repo), so no chat gate applies. The
    caller has already registered the operation; this ends it.
    """
    try:
        if is_destination_change:
            change_backup_destination_for_host(
                agent_id=agent_id,
                agent_address=agent_address,
                request=request,
                imbue_cloud_cli=imbue_cloud_cli,
                paths=paths,
                parent_cg=parent_cg,
                quota_evictor=quota_evictor,
            )
        else:
            configure_backups_for_host(
                agent_id=agent_id,
                agent_address=agent_address,
                request=request,
                imbue_cloud_cli=imbue_cloud_cli,
                paths=paths,
                parent_cg=parent_cg,
                quota_evictor=quota_evictor,
            )
    except ResticPasswordNotAllowedError as exc:
        logger.warning("Backup configure for {} refused its settings: {}", agent_id, exc)
        registry.fail_with_reason(agent_id, str(exc), OperationFailureReason.RESTIC_PASSWORD_SET)
        return
    except ImbueCloudQuotaExceededCliError as exc:
        logger.warning("Backup configure for {} was refused by the account's quota: {}", agent_id, exc)
        registry.fail_with_reason(agent_id, str(exc), OperationFailureReason.STORAGE_QUOTA_EXCEEDED)
        return
    except (BackupProvisioningError, ImbueCloudCliError) as exc:
        logger.warning("Backup configure for {} failed: {}", agent_id, exc)
        registry.fail(agent_id, str(exc))
        return
    registry.complete(agent_id)


def run_backup_disable_sequence(
    *,
    agent_id: AgentId,
    # How ``mngr`` reaches the workspace (see ``build_agent_address``)
    agent_address: str,
    paths: InstallationPaths,
    parent_cg: ConcurrencyGroup | None,
    registry: WorkspaceOperationRegistryInterface,
) -> None:
    """Worker-thread entry point for the disable-backups operation.

    Env-only (archives the canonical env and rotates the workspace copy
    aside), so no chat gate applies. The caller has already registered the
    operation; this ends it.
    """
    try:
        disable_backups_for_host(agent_id=agent_id, agent_address=agent_address, paths=paths, parent_cg=parent_cg)
    except BackupProvisioningError as exc:
        logger.warning("Backup disable for {} failed: {}", agent_id, exc)
        registry.fail_with_reason(agent_id, str(exc), OperationFailureReason.DISABLE_FAILED)
        return
    registry.complete(agent_id)
