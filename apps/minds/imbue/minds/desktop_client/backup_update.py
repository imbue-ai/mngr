"""The one idempotent "Update backup service" operation for a workspace.

Converges everything the verification check can find: the backup-service code
(checked out at the target ``minds-v*`` tag and committed as
``backup-update: <tag>``), the injected ``restic.env`` (re-injected from the
canonical copy, rotating a drifted workspace copy aside), and the supervisord
service (restarted and verified RUNNING by the apply script).

Runs as a tracked workspace operation (the ``workspace_operations`` registry,
like restart) so the settings view can poll step-level progress:

1. Gate + wait: poll the workspace's gate probe until no backup tick is in
   flight. Actively-RUNNING chat agents block the *code* path -- the caller
   passes ``is_stop_chats`` (the "Stop all chats and retry" flow) to have the
   apply script stop them first. Waiting is unbounded and cancellable (the
   cancel just stops polling; nothing has been mutated yet).
2. Apply: one exec runs the mutating script (stash / checkout tag / commit /
   ``uv sync`` / restart / verify), which auto-rolls-back via ``git revert``
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
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.build_info import resolve_release_id
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client import backup_status
from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backup_env_store import has_canonical_env
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import change_backup_destination_for_host
from imbue.minds.desktop_client.backup_provisioning import configure_backups_for_host
from imbue.minds.desktop_client.backup_provisioning import disable_backups_for_host
from imbue.minds.desktop_client.backup_provisioning import reinject_canonical_env
from imbue.minds.desktop_client.backup_provisioning import run_mngr_exec_on_agent
from imbue.minds.desktop_client.backup_verification import BackupServiceCheckState
from imbue.minds.desktop_client.backup_verification import BackupServiceProblem
from imbue.minds.desktop_client.backup_verification import check_backup_service_for_workspace
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_APPLY_UPDATE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_GATE_PROBE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_RESTORE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import GATE_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import RESTORE_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import UPDATE_RESULT_MARKER
from imbue.minds.desktop_client.backup_workspace_scripts import build_workspace_script_command
from imbue.minds.desktop_client.backup_workspace_scripts import extract_marker_json
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.workspace_operations import WorkspaceOperationRegistryInterface
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId

# Machine-readable prefix on the operation error when running chats block the
# update; the UI parses the comma-separated chat names after it.
BLOCKED_BY_RUNNING_CHATS_PREFIX: Final[str] = "BLOCKED_BY_RUNNING_CHATS:"

# User-facing guidance when the apply script's `git stash pop` conflicted;
# shown on both the success (warning log) and failure (error message) paths so
# stashed changes never look lost.
_STASH_CONFLICT_GUIDANCE: Final[str] = (
    "Your uncommitted changes could not be restored automatically; "
    "they are preserved in the git stash (run `git stash pop` in the workspace)."
)

# Must exceed the gate probe script's own `uv run mngr list` budget (180s)
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
# Ceiling for the best-effort `supervisorctl restart all` dispatched when the
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
    paths: WorkspacePaths,
    resolver: BackendResolverInterface,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    is_stop_chats: bool,
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
            registry=registry,
            parent_cg=parent_cg,
            is_stop_chats=is_stop_chats,
        )
    except BackupProvisioningError as exc:
        logger.warning("Backup update for {} failed: {}", agent_id, exc)
        registry.fail(agent_id, str(exc))


def _run_update_phases(
    *,
    agent_id: AgentId,
    paths: WorkspacePaths,
    resolver: BackendResolverInterface,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    is_stop_chats: bool,
) -> None:
    # Phase 1: gate + wait (cancellable; nothing has been mutated yet).
    registry.append_log(agent_id, "Checking for running chats and in-progress backups...")
    if not _wait_for_quiet_workspace(
        agent_id=agent_id,
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
        registry=registry,
        parent_cg=parent_cg,
        is_stop_chats=is_stop_chats,
    )
    if update_error is not None:
        registry.fail(agent_id, update_error)
        return
    registry.complete(agent_id)


def _make_exec_log_forwarder(
    registry: WorkspaceOperationRegistryInterface, agent_id: AgentId
) -> Callable[[str, bool], None]:
    """Build an ``on_output`` callback that streams exec output into the operation log.

    The scripts' marker verdict line is excluded -- it is machine-readable
    plumbing, parsed separately -- and blank lines are dropped. Everything
    else (the restore script's phase/progress lines, stray warnings, crash
    tracebacks on stderr) lands in the log so the user can follow along live.
    """

    def _forward(line: str, is_stdout: bool) -> None:
        stripped = line.rstrip()
        if not stripped.strip():
            return
        if stripped.startswith(RESTORE_RESULT_MARKER) or stripped.startswith(UPDATE_RESULT_MARKER):
            return
        registry.append_log(agent_id, stripped)

    return _forward


def _apply_update_and_verify(
    *,
    agent_id: AgentId,
    paths: WorkspacePaths,
    resolver: BackendResolverInterface,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    is_stop_chats: bool,
) -> str | None:
    """Run the mutating update script, re-inject the env, and verify convergence.

    Returns the failure detail (possibly ``BLOCKED_BY_RUNNING_CHATS_PREFIX``-
    prefixed), or None on success. Shared by the standalone update operation
    (which fails the operation on error) and the restore's chained update
    (which downgrades an error to a completion warning).
    """
    # The mutating apply script (stash/checkout/commit/sync/restart).
    registry.append_log(agent_id, "Applying the backup service update...")
    apply_command = build_workspace_script_command(
        BACKUP_APPLY_UPDATE_SCRIPT,
        (
            "--minds-version",
            resolve_release_id(),
            "--agent-id",
            str(agent_id),
        )
        + (("--stop-chats",) if is_stop_chats else ()),
    )
    apply_result = run_mngr_exec_on_agent(
        agent_id,
        apply_command,
        parent_cg=parent_cg,
        timeout_seconds=_APPLY_EXEC_TIMEOUT_SECONDS,
        on_output=_make_exec_log_forwarder(registry, agent_id),
    )
    payload = extract_marker_json(apply_result.stdout, UPDATE_RESULT_MARKER)
    if payload is None:
        detail = (apply_result.stderr or apply_result.stdout).strip()[-800:]
        return f"The update script produced no result: {detail}"
    status = str(payload.get("status", "failed"))
    if status == "blocked":
        running_chats = payload.get("running_chats")
        chat_names = ",".join(str(name) for name in running_chats) if isinstance(running_chats, list) else ""
        return f"{BLOCKED_BY_RUNNING_CHATS_PREFIX}{chat_names}"
    if status != "ok":
        detail = str(payload.get("detail", "unknown failure"))
        rolled_back_note = " (changes were rolled back)" if payload.get("rolled_back") else ""
        stash_note = f" {_STASH_CONFLICT_GUIDANCE}" if payload.get("stash_conflict") else ""
        return f"{detail}{rolled_back_note}{stash_note}"
    if payload.get("committed"):
        registry.append_log(agent_id, f"Updated backup service code to {payload.get('tag', 'the target tag')}.")
    else:
        registry.append_log(agent_id, "Backup service code already matched the target version.")
    if payload.get("stash_conflict"):
        registry.append_log(agent_id, f"Warning: {_STASH_CONFLICT_GUIDANCE}")

    # Re-inject the canonical env (rotates a drifted workspace copy).
    if has_canonical_env(paths, agent_id):
        registry.append_log(agent_id, "Re-injecting backup credentials...")
        reinject_canonical_env(agent_id=agent_id, paths=paths, parent_cg=parent_cg)

    # Verify convergence with a fresh check.
    registry.append_log(agent_id, "Verifying the backup service...")
    check = check_backup_service_for_workspace(paths, agent_id, resolver=resolver, parent_cg=parent_cg)
    failing = tuple(problem for problem in check.problems if problem in _PROBLEMS_THAT_FAIL_UPDATE)
    if check.state == BackupServiceCheckState.PROBLEMS and failing:
        problem_names = ", ".join(problem.value for problem in failing)
        return f"The update ran but verification still reports: {problem_names}. {check.detail}"
    if BackupServiceProblem.NOT_CONFIGURED in check.problems:
        registry.append_log(
            agent_id, "Backups are still not configured for this workspace; enable them from the backup settings."
        )
    return None


def _wait_for_quiet_workspace(
    *,
    agent_id: AgentId,
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
    probe_command = build_workspace_script_command(BACKUP_GATE_PROBE_SCRIPT, ("--agent-id", str(agent_id)))
    is_waiting_logged = False
    is_gate_error_logged = False
    while not registry.is_cancel_requested(agent_id):
        probe_result = run_mngr_exec_on_agent(
            agent_id, probe_command, parent_cg=parent_cg, timeout_seconds=_GATE_PROBE_TIMEOUT_SECONDS
        )
        payload = extract_marker_json(probe_result.stdout, GATE_RESULT_MARKER)
        if payload is None:
            detail = (probe_result.stderr or probe_result.stdout).strip()[-500:]
            registry.fail(agent_id, f"Could not probe the workspace: {detail}")
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
    paths: WorkspacePaths,
    resolver: BackendResolverInterface,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    snapshot_id: str,
    is_stop_chats: bool,
    is_update_after: bool,
    is_skip_safety_snapshot: bool,
    is_skip_chat_gate: bool,
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
            registry=registry,
            parent_cg=parent_cg,
            snapshot_id=snapshot_id,
            is_stop_chats=is_stop_chats,
            is_update_after=is_update_after,
            is_skip_safety_snapshot=is_skip_safety_snapshot,
            is_skip_chat_gate=is_skip_chat_gate,
        )
    except BackupProvisioningError as exc:
        logger.warning("Backup restore for {} failed: {}", agent_id, exc)
        registry.fail(agent_id, str(exc))


def _resolve_restore_snapshot(
    *,
    agent_id: AgentId,
    paths: WorkspacePaths,
    snapshot_id: str,
    parent_cg: ConcurrencyGroup | None,
) -> restic_cli.ResticSnapshot:
    """Resolve the snapshot to restore, from minds' own view of the repository.

    minds holds the canonical restic.env, so it can read the snapshot's
    recorded root and timestamp here -- the in-workspace script does not need
    to re-query restic for metadata minds already has. Doing it before the
    workspace is touched also means a bad snapshot id fails the operation
    outright, rather than after the services are stopped and a safety snapshot
    has been taken for nothing.
    """
    snapshots = backup_status.list_workspace_snapshots(
        paths, agent_id, parent_cg=parent_cg, timeout_seconds=_SNAPSHOT_RESOLVE_TIMEOUT_SECONDS
    )
    for snapshot in snapshots:
        if snapshot_id in (snapshot.snapshot_id, snapshot.short_id):
            if not snapshot.paths:
                raise BackupProvisioningError(f"Snapshot {snapshot_id} records no paths; it cannot be restored")
            return snapshot
    raise BackupProvisioningError(f"No backup {snapshot_id} exists for this workspace")


def _resolve_restore_subpath(
    *,
    agent_id: AgentId,
    paths: WorkspacePaths,
    snapshot: restic_cli.ResticSnapshot,
    parent_cg: ConcurrencyGroup | None,
) -> str:
    """Locate the host-dir subtree inside the snapshot (identified by its ``code/`` checkout).

    On plain docker the snapshot root *is* the host dir; on btrfs providers
    the hourly backup snapshots the whole unified host volume, so the host
    dir's contents live one level down in a ``host_dir/`` child (next to
    volume-level ``agents/`` + ``host_state.json``). Resolved here, from
    minds' own view of the repository, so the in-workspace script only ever
    consumes a validated ``<snapshot>:<subpath>`` -- restoring the wrong
    level would wreck the workspace.
    """
    root = snapshot.paths[0]
    root_entries = backup_status.list_workspace_snapshot_directory(
        paths,
        agent_id,
        snapshot_id=snapshot.snapshot_id,
        directory=root,
        parent_cg=parent_cg,
        timeout_seconds=_SNAPSHOT_RESOLVE_TIMEOUT_SECONDS,
    )
    if f"{root}/code" in root_entries:
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
        )
        if f"{nested_root}/code" in nested_entries:
            return nested_root
    raise BackupProvisioningError(
        f"Snapshot {snapshot.short_id} does not contain a workspace (no code/ checkout); it cannot be restored"
    )


def _chained_update_warning(update_error: str) -> str:
    """Word a chained-update failure as a completion warning (the restore itself succeeded)."""
    if update_error.startswith(BLOCKED_BY_RUNNING_CHATS_PREFIX):
        names = update_error[len(BLOCKED_BY_RUNNING_CHATS_PREFIX) :]
        names_note = f" ({names})" if names else ""
        return (
            f"The restore succeeded, but the backup service update afterwards was blocked by running "
            f'chats{names_note}. Run "Update backup software" from Settings once they are stopped.'
        )
    return (
        f"The restore succeeded, but updating the backup service afterwards failed: {update_error} "
        'You can retry it from Settings with "Update backup software".'
    )


def _run_restore_phases(
    *,
    agent_id: AgentId,
    paths: WorkspacePaths,
    resolver: BackendResolverInterface,
    registry: WorkspaceOperationRegistryInterface,
    parent_cg: ConcurrencyGroup | None,
    snapshot_id: str,
    is_stop_chats: bool,
    is_update_after: bool,
    is_skip_safety_snapshot: bool,
    is_skip_chat_gate: bool,
) -> None:
    # Phase 0: resolve the snapshot and its host-dir subpath before anything
    # waits or mutates, so an unknown id (or a snapshot with no workspace in
    # it) fails fast and cheaply.
    snapshot = _resolve_restore_snapshot(agent_id=agent_id, paths=paths, snapshot_id=snapshot_id, parent_cg=parent_cg)
    snapshot_subpath = _resolve_restore_subpath(agent_id=agent_id, paths=paths, snapshot=snapshot, parent_cg=parent_cg)

    # Phase 0.5: converge the workspace's restic.env to the canonical copy
    # before dispatch -- the restore script reads its credentials from the
    # workspace file, and precisely the workspaces most in need of a restore
    # (ENV_MISSING / ENV_MISMATCH) would otherwise fail. A differing
    # workspace copy is archived aside, never destroyed. Also proves, before
    # anything mutates, that the snapshot the user picked lives in the same
    # repository the script will read: both came from the canonical env.
    registry.append_log(agent_id, "Making sure the workspace has the right backup credentials...")
    reinject_canonical_env(agent_id=agent_id, paths=paths, parent_cg=parent_cg)

    # Phase 1: gate + wait (cancellable; nothing has been mutated yet). Kept
    # even for a forced restore: the probe tolerates a broken `mngr list`
    # (gate_error) by design, and real knowledge of running chats must still
    # block -- the force flag only skips the check the workspace can no
    # longer answer.
    registry.append_log(agent_id, "Checking for running chats and in-progress backups...")
    if not _wait_for_quiet_workspace(
        agent_id=agent_id,
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
    # operation log as it runs.
    registry.append_log(
        agent_id, "Backing up the current state, then restoring the selected backup. This can take a while..."
    )
    restore_command = build_workspace_script_command(
        BACKUP_RESTORE_SCRIPT,
        (
            "--agent-id",
            str(agent_id),
            "--snapshot-id",
            snapshot_id,
            # Resolved above from minds' own view of the repository, so the
            # script never re-queries restic for this metadata.
            "--snapshot-subpath",
            snapshot_subpath,
            "--source-time",
            snapshot.time.isoformat(),
        )
        + (("--stop-chats",) if is_stop_chats else ())
        + (("--skip-chat-gate",) if is_skip_chat_gate else ())
        + (("--skip-safety-snapshot",) if is_skip_safety_snapshot else ()),
    )
    restore_result = run_mngr_exec_on_agent(
        agent_id,
        restore_command,
        parent_cg=parent_cg,
        timeout_seconds=_RESTORE_EXEC_TIMEOUT_SECONDS,
        on_output=_make_exec_log_forwarder(registry, agent_id),
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
            agent_id,
            "supervisorctl restart all",
            parent_cg=parent_cg,
            timeout_seconds=_SERVICE_RESUME_TIMEOUT_SECONDS,
        )
        if resume_result.returncode != 0:
            resume_detail = (resume_result.stderr or resume_result.stdout).strip()[-300:]
            logger.warning("Post-failure service resume for {} failed: {}", agent_id, resume_detail)
        registry.fail(agent_id, f"The restore script produced no result: {detail}")
        return
    status = str(payload.get("status", "failed"))
    if status == "blocked":
        running_chats = payload.get("running_chats")
        chat_names = ",".join(str(name) for name in running_chats) if isinstance(running_chats, list) else ""
        registry.fail(agent_id, f"{BLOCKED_BY_RUNNING_CHATS_PREFIX}{chat_names}")
        return
    if status != "ok":
        detail = str(payload.get("detail", "unknown failure"))
        safety_note = (
            " A safety backup of the pre-restore state was taken first."
            if payload.get("safety_snapshot_taken")
            else ""
        )
        registry.fail(agent_id, f"{detail}{safety_note}")
        return
    registry.append_log(
        agent_id, "Restored the backup, reinstalled dependencies, and restarted the workspace services."
    )

    # Phase 3: the script wrote back the pre-restore restic.env, but re-inject
    # the canonical copy anyway so the workspace ends converged even if the
    # env had drifted before the restore.
    if has_canonical_env(paths, agent_id):
        registry.append_log(agent_id, "Re-injecting backup credentials...")
        reinject_canonical_env(agent_id=agent_id, paths=paths, parent_cg=parent_cg)

    # Phase 4 (default-on): converge the backup-service code afterwards. The
    # restored snapshot may carry arbitrarily old libs/host_backup code; the
    # idempotent update brings it back to the current version. Its failure
    # must not fail the operation -- the user's data is restored, which is
    # what they asked for -- so it downgrades to a completion warning.
    if not is_update_after:
        registry.complete(agent_id)
        return
    registry.append_log(agent_id, "Updating the backup service to the current version...")
    update_error = _apply_update_and_verify(
        agent_id=agent_id,
        paths=paths,
        resolver=resolver,
        registry=registry,
        parent_cg=parent_cg,
        is_stop_chats=is_stop_chats,
    )
    if update_error is None:
        registry.complete(agent_id)
        return
    logger.warning("Chained backup-service update after restore for {} failed: {}", agent_id, update_error)
    registry.complete_with_warning(agent_id, _chained_update_warning(update_error))


def run_backup_configure_sequence(
    *,
    agent_id: AgentId,
    host_id: str,
    request: BackupSetupRequest,
    imbue_cloud_cli: ImbueCloudCli | None,
    paths: WorkspacePaths,
    parent_cg: ConcurrencyGroup | None,
    registry: WorkspaceOperationRegistryInterface,
    is_destination_change: bool,
) -> None:
    """Worker-thread entry point for the enable / change-destination operation.

    Env-only (never touches the workspace repo), so no chat gate applies. The
    caller has already registered the operation; this ends it.
    """
    try:
        if is_destination_change:
            change_backup_destination_for_host(
                agent_id=agent_id,
                host_id=host_id,
                request=request,
                imbue_cloud_cli=imbue_cloud_cli,
                paths=paths,
                parent_cg=parent_cg,
            )
        else:
            configure_backups_for_host(
                agent_id=agent_id,
                host_id=host_id,
                request=request,
                imbue_cloud_cli=imbue_cloud_cli,
                paths=paths,
                parent_cg=parent_cg,
            )
    except (BackupProvisioningError, ImbueCloudCliError) as exc:
        logger.warning("Backup configure for {} failed: {}", agent_id, exc)
        registry.fail(agent_id, str(exc))
        return
    registry.complete(agent_id)


def run_backup_disable_sequence(
    *,
    agent_id: AgentId,
    paths: WorkspacePaths,
    parent_cg: ConcurrencyGroup | None,
    registry: WorkspaceOperationRegistryInterface,
) -> None:
    """Worker-thread entry point for the disable-backups operation.

    Env-only (archives the canonical env and rotates the workspace copy
    aside), so no chat gate applies. The caller has already registered the
    operation; this ends it.
    """
    try:
        disable_backups_for_host(agent_id=agent_id, paths=paths, parent_cg=parent_cg)
    except BackupProvisioningError as exc:
        logger.warning("Backup disable for {} failed: {}", agent_id, exc)
        registry.fail(agent_id, str(exc))
        return
    registry.complete(agent_id)
