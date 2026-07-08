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

from typing import Final

from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.build_info import resolve_release_id
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backup_env_store import has_canonical_env
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import change_backup_destination_for_host
from imbue.minds.desktop_client.backup_provisioning import configure_backups_for_host
from imbue.minds.desktop_client.backup_provisioning import reinject_canonical_env
from imbue.minds.desktop_client.backup_provisioning import run_mngr_exec_on_agent
from imbue.minds.desktop_client.backup_verification import BackupServiceCheckState
from imbue.minds.desktop_client.backup_verification import BackupServiceProblem
from imbue.minds.desktop_client.backup_verification import check_backup_service_for_workspace
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_APPLY_UPDATE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_GATE_PROBE_SCRIPT
from imbue.minds.desktop_client.backup_workspace_scripts import GATE_RESULT_MARKER
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

    # Phase 2: the mutating apply script (stash/checkout/commit/sync/restart).
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
        agent_id, apply_command, parent_cg=parent_cg, timeout_seconds=_APPLY_EXEC_TIMEOUT_SECONDS
    )
    payload = extract_marker_json(apply_result.stdout, UPDATE_RESULT_MARKER)
    if payload is None:
        detail = (apply_result.stderr or apply_result.stdout).strip()[-800:]
        registry.fail(agent_id, f"The update script produced no result: {detail}")
        return
    status = str(payload.get("status", "failed"))
    if status == "blocked":
        running_chats = payload.get("running_chats")
        chat_names = ",".join(str(name) for name in running_chats) if isinstance(running_chats, list) else ""
        registry.fail(agent_id, f"{BLOCKED_BY_RUNNING_CHATS_PREFIX}{chat_names}")
        return
    if status != "ok":
        detail = str(payload.get("detail", "unknown failure"))
        rolled_back_note = " (changes were rolled back)" if payload.get("rolled_back") else ""
        registry.fail(agent_id, f"{detail}{rolled_back_note}")
        return
    if payload.get("committed"):
        registry.append_log(agent_id, f"Updated backup service code to {payload.get('tag', 'the target tag')}.")
    else:
        registry.append_log(agent_id, "Backup service code already matched the target version.")
    if payload.get("stash_conflict"):
        registry.append_log(
            agent_id,
            "Warning: your uncommitted changes could not be restored automatically; "
            "they are preserved in the git stash (run `git stash pop` in the workspace).",
        )

    # Phase 3: re-inject the canonical env (rotates a drifted workspace copy).
    if has_canonical_env(paths, agent_id):
        registry.append_log(agent_id, "Re-injecting backup credentials...")
        reinject_canonical_env(agent_id=agent_id, paths=paths, parent_cg=parent_cg)

    # Phase 4: verify convergence with a fresh check.
    registry.append_log(agent_id, "Verifying the backup service...")
    check = check_backup_service_for_workspace(paths, agent_id, resolver=resolver, parent_cg=parent_cg)
    failing = tuple(problem for problem in check.problems if problem in _PROBLEMS_THAT_FAIL_UPDATE)
    if check.state == BackupServiceCheckState.PROBLEMS and failing:
        problem_names = ", ".join(problem.value for problem in failing)
        registry.fail(agent_id, f"The update ran but verification still reports: {problem_names}. {check.detail}")
        return
    if BackupServiceProblem.NOT_CONFIGURED in check.problems:
        registry.append_log(
            agent_id, "Backups are still not configured for this workspace; enable them from the backup settings."
        )
    registry.complete(agent_id)


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
    registry.fail(agent_id, "Cancelled before any changes were made.")
    return False


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
