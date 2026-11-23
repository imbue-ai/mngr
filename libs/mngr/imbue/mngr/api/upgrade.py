import shlex
import tempfile
from collections.abc import Generator
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from enum import auto
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.upgrade import AGENT_DATA_RELATIVE_PATH
from imbue.mngr.api.discovery_events import emit_discovery_events_for_host
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import START_HOST_LOCK_TIMEOUT_SECONDS
from imbue.mngr.api.providers import get_local_host
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import InvalidUpgradeInputError
from imbue.mngr.errors import LockNotHeldError
from imbue.mngr.errors import MessageLockTimeoutError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.common import check_agent_type_known
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import write_shared_shell_libs
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import UpgradableAgentMixin
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import HarnessFileSpec
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import UpgradeSkipDetail
from imbue.mngr.utils.deps import RSYNC

UPGRADE_STAGING_DIR_NAME: Final[str] = "upgrade_staging"

# Long enough for a send to an idle agent to submit, its Enter retries included.
UPGRADE_MESSAGE_LOCK_TIMEOUT_SECONDS: Final[float] = 45.0

_HOST_FILE_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0

_SILENT_HOST_COMMAND_FAILURE_MESSAGE: Final[str] = "the host command failed without an error message"

_RECORD_MISSING_SWAP_OUTPUT: Final[str] = "agent-record-missing"


class UpgradePlan(UpperCaseStrEnum):
    """What ``mngr upgrade`` does with an agent around rewriting its files."""

    LEAVE_STOPPED = auto()
    RESTART_IDLE = auto()
    RESTART_INTERRUPTING = auto()


class UpgradeFailedStep(UpperCaseStrEnum):
    """The step of ``mngr upgrade`` that failed for an agent."""

    LOCK = auto()
    PREPARE = auto()
    START_CHECK = auto()
    STOP = auto()
    SWAP = auto()
    START = auto()
    READY = auto()


class UpgradeSkipKind(UpperCaseStrEnum):
    """What a skip by ``mngr upgrade`` is about, one subject per kind: the agent's state (try again later, or force it; a destroyed agent is reported here too, and needs neither), what the agent is (needs a person) or the run (run it again)."""

    NOT_IDLE = auto()
    UNSUPPORTED = auto()
    ABORTED = auto()


class UpgradeSkip(FrozenModel):
    """Why ``mngr upgrade`` leaves an agent as it was: the sentence its line prints and the kind a program reads."""

    kind: UpgradeSkipKind = Field(description="What the skip is about")
    detail: UpgradeSkipDetail = Field(description="Why the agent was left untouched")


SKIP_ON_DIALOG: Final[UpgradeSkip] = UpgradeSkip(
    kind=UpgradeSkipKind.NOT_IDLE, detail=UpgradeSkipDetail("it is on a dialog (--force interrupts it)")
)
SKIP_MID_TURN: Final[UpgradeSkip] = UpgradeSkip(
    kind=UpgradeSkipKind.NOT_IDLE, detail=UpgradeSkipDetail("it is mid-turn (--force interrupts it)")
)
SKIP_BACKGROUND_WORK: Final[UpgradeSkip] = UpgradeSkip(
    kind=UpgradeSkipKind.NOT_IDLE, detail=UpgradeSkipDetail("it is running background work (--force interrupts it)")
)
SKIP_REPLACED: Final[UpgradeSkip] = UpgradeSkip(
    kind=UpgradeSkipKind.NOT_IDLE,
    detail=UpgradeSkipDetail("another program is running in its terminal (--force ends it and restarts the agent)"),
)
SKIP_EXITED: Final[UpgradeSkip] = UpgradeSkip(
    kind=UpgradeSkipKind.NOT_IDLE, detail=UpgradeSkipDetail("its program has exited (--force restarts it)")
)
SKIP_STATE_UNREADABLE: Final[UpgradeSkip] = UpgradeSkip(
    kind=UpgradeSkipKind.NOT_IDLE, detail=UpgradeSkipDetail("its state could not be read")
)
SKIP_IS_CALLER: Final[UpgradeSkip] = UpgradeSkip(
    kind=UpgradeSkipKind.NOT_IDLE, detail=UpgradeSkipDetail("it is running this command")
)
SKIP_REMOTE: Final[UpgradeSkip] = UpgradeSkip(
    kind=UpgradeSkipKind.UNSUPPORTED,
    detail=UpgradeSkipDetail("it is on another host (only agents on the local host are upgraded)"),
)
SKIP_DESTROYED: Final[UpgradeSkip] = UpgradeSkip(
    kind=UpgradeSkipKind.NOT_IDLE, detail=UpgradeSkipDetail("it was destroyed during the run")
)
SKIP_ABORTED: Final[UpgradeSkip] = UpgradeSkip(
    kind=UpgradeSkipKind.ABORTED,
    detail=UpgradeSkipDetail("not attempted, because an earlier agent failed"),
)


class AgentUpgraded(FrozenModel):
    """An agent whose files ``mngr upgrade`` rewrote, or would rewrite in a dry run."""

    agent_id: AgentId = Field(description="The agent's id")
    agent_name: AgentName = Field(description="The agent's name")
    plan: UpgradePlan = Field(description="What was done with the agent around the rewrite")
    prior_state: AgentLifecycleState = Field(description="The agent's state before the upgrade")
    changed_paths: tuple[Path, ...] = Field(
        description="The rewritten files, relative to the agent's state dir, sorted"
    )
    is_dry_run: bool = Field(description="Whether this comes from a dry run, which changes nothing")


class AgentCurrent(FrozenModel):
    """An agent whose files already match what ``mngr upgrade`` writes."""

    agent_id: AgentId = Field(description="The agent's id")
    agent_name: AgentName = Field(description="The agent's name")
    is_dry_run: bool = Field(description="Whether this comes from a dry run, which changes nothing")


class AgentUpgradeSkipped(FrozenModel):
    """An agent ``mngr upgrade`` left out of date and untouched."""

    agent_id: AgentId = Field(description="The agent's id")
    agent_name: AgentName = Field(description="The agent's name")
    kind: UpgradeSkipKind = Field(description="What the skip is about")
    detail: UpgradeSkipDetail = Field(description="Why the agent was left untouched")
    is_dry_run: bool = Field(description="Whether this comes from a dry run, which changes nothing")


class AgentUpgradeFailed(FrozenModel):
    """An agent for which a step of ``mngr upgrade`` failed."""

    agent_id: AgentId = Field(description="The agent's id")
    agent_name: AgentName = Field(description="The agent's name")
    step: UpgradeFailedStep = Field(description="The step that failed")
    error_message: str = Field(description="What went wrong")


AgentUpgradeOutcome = AgentUpgraded | AgentCurrent | AgentUpgradeSkipped | AgentUpgradeFailed


class _UpgradeToApply(FrozenModel):
    """What ``mngr upgrade`` would change for an agent: the files this mngr builds for it."""

    input_content_by_path: dict[Path, str | None] = Field(
        description="The content of each input the files were built from, None for a missing one"
    )
    upgraded_files: dict[Path, HarnessFileSpec] = Field(description="Every file this mngr generates for the agent")
    ready_signal_paths: tuple[Path, ...] = Field(description="The readiness markers to remove while the agent is down")


class _HostCommandFailedError(MngrError):
    """A host command that ``mngr upgrade`` runs exited with an error."""


class _PathOutsideStateDirError(MngrError):
    """A harness named a path that is absolute or leaves the agent's state dir."""


@pure
def plan_agent_upgrade(
    state: AgentLifecycleState, is_blocked_on_dialog: bool, is_forced: bool
) -> UpgradePlan | UpgradeSkip:
    """What upgrade does with an agent in ``state``, or why it skips it; a busy or exited agent is restarted only when forced."""
    match state:
        case AgentLifecycleState.STOPPED:
            return UpgradePlan.LEAVE_STOPPED
        case AgentLifecycleState.WAITING if not is_blocked_on_dialog:
            return UpgradePlan.RESTART_IDLE
        case AgentLifecycleState.WAITING:
            return UpgradePlan.RESTART_INTERRUPTING if is_forced else SKIP_ON_DIALOG
        case AgentLifecycleState.RUNNING:
            return UpgradePlan.RESTART_INTERRUPTING if is_forced else SKIP_MID_TURN
        case AgentLifecycleState.RUNNING_BACKGROUND:
            return UpgradePlan.RESTART_INTERRUPTING if is_forced else SKIP_BACKGROUND_WORK
        case AgentLifecycleState.REPLACED:
            return UpgradePlan.RESTART_INTERRUPTING if is_forced else SKIP_REPLACED
        case AgentLifecycleState.DONE:
            return UpgradePlan.RESTART_INTERRUPTING if is_forced else SKIP_EXITED
        case AgentLifecycleState.UNKNOWN | AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE:
            return SKIP_STATE_UNREADABLE
        case _ as unreachable:
            assert_never(unreachable)


def upgrade_agents(
    matches: Sequence[AgentMatch],
    is_forced: bool,
    is_dry_run: bool,
    error_behavior: ErrorBehavior,
    caller_agent_id: AgentId | None,
    mngr_ctx: MngrContext,
) -> Iterator[AgentUpgradeOutcome]:
    """Upgrade each matched agent in the given order, yielding each outcome; after a failure, ``error_behavior`` says whether the run goes on."""
    local_matches = [match for match in matches if match.provider_name == LOCAL_PROVIDER_NAME]
    local_host = get_local_host(mngr_ctx) if local_matches else None
    agent_by_id = {agent.id: agent for agent in local_host.get_agents()} if local_host is not None else {}
    return iter_upgrade_outcomes(
        matches,
        lambda match: upgrade_matched_agent(
            match, local_host, agent_by_id, is_forced, is_dry_run, caller_agent_id, mngr_ctx
        ),
        error_behavior,
        is_dry_run,
    )


def iter_upgrade_outcomes(
    matches: Sequence[AgentMatch],
    upgrade_one_agent: Callable[[AgentMatch], AgentUpgradeOutcome],
    error_behavior: ErrorBehavior,
    is_dry_run: bool,
) -> Iterator[AgentUpgradeOutcome]:
    """Yield ``upgrade_one_agent`` for each match; after a failure under ``ErrorBehavior.ABORT``, a skip for each match after it."""
    for idx, agent_match in enumerate(matches):
        outcome = upgrade_one_agent(agent_match)
        yield outcome
        if not isinstance(outcome, AgentUpgradeFailed):
            continue
        match error_behavior:
            case ErrorBehavior.CONTINUE:
                pass
            case ErrorBehavior.ABORT:
                for unreached_match in matches[idx + 1 :]:
                    yield _skipped(unreached_match.agent_id, unreached_match.agent_name, SKIP_ABORTED, is_dry_run)
                return
            case _ as unreachable:
                assert_never(unreachable)


def upgrade_matched_agent(
    match: AgentMatch,
    # None when no match is on the local host.
    local_host: OnlineHostInterface | None,
    agent_by_id: Mapping[AgentId, AgentInterface[Any]],
    is_forced: bool,
    is_dry_run: bool,
    caller_agent_id: AgentId | None,
    mngr_ctx: MngrContext,
) -> AgentUpgradeOutcome:
    if local_host is None or match.provider_name != LOCAL_PROVIDER_NAME:
        return _skipped(match.agent_id, match.agent_name, SKIP_REMOTE, is_dry_run)
    agent = agent_by_id.get(match.agent_id)
    if agent is None:
        return _skipped(match.agent_id, match.agent_name, SKIP_DESTROYED, is_dry_run)
    return upgrade_agent(agent, local_host, is_forced, is_dry_run, match.agent_id == caller_agent_id, mngr_ctx)


def upgrade_agent(
    agent: AgentInterface[Any],
    host: OnlineHostInterface,
    is_forced: bool,
    is_dry_run: bool,
    # Whether the agent is the one running this command.
    is_caller: bool,
    mngr_ctx: MngrContext,
) -> AgentUpgradeOutcome:
    """Rewrite the agent's generated files and restart it if idle (or busy, when forced); a dry run only reports."""
    if not isinstance(agent, UpgradableAgentMixin):
        return _skipped(agent.id, agent.name, _describe_missing_upgrade_support(agent, mngr_ctx), is_dry_run)
    outcome = _run_agent_upgrade(agent, agent, host, is_forced, is_dry_run, is_caller, mngr_ctx)
    return _skip_if_destroyed(outcome, agent, host, is_dry_run)


def _run_agent_upgrade(
    agent: AgentInterface[Any],
    # The same agent, as the view that builds its upgraded files.
    upgradable_agent: UpgradableAgentMixin,
    host: OnlineHostInterface,
    is_forced: bool,
    is_dry_run: bool,
    is_caller: bool,
    mngr_ctx: MngrContext,
) -> AgentUpgradeOutcome:
    try:
        with host.lock_cooperatively(timeout_seconds=START_HOST_LOCK_TIMEOUT_SECONDS):
            outcome = _upgrade_agent_holding_lock(agent, upgradable_agent, host, is_forced, is_dry_run, is_caller)
    except LockNotHeldError as e:
        return _failed(agent, UpgradeFailedStep.LOCK, e)

    if _is_agent_stop_attempted(outcome):
        emit_discovery_events_for_host(mngr_ctx.config, host)
    if isinstance(outcome, AgentUpgraded) and not outcome.is_dry_run and _is_restarting_plan(outcome.plan):
        return _wait_for_restarted_agent(agent, upgradable_agent, outcome)
    return outcome


def _skip_if_destroyed(
    outcome: AgentUpgradeOutcome, agent: AgentInterface[Any], host: OnlineHostInterface, is_dry_run: bool
) -> AgentUpgradeOutcome:
    """``outcome``, or the destroyed skip when the agent's record is gone and its files were not rewritten."""
    if not _is_outcome_void_for_destroyed_agent(outcome):
        return outcome
    if host.path_exists(get_agent_state_dir_path(host.host_dir, agent.id) / AGENT_DATA_RELATIVE_PATH):
        return outcome
    return _skipped(agent.id, agent.name, SKIP_DESTROYED, is_dry_run)


def _upgrade_agent_holding_lock(
    agent: AgentInterface[Any],
    upgradable_agent: UpgradableAgentMixin,
    host: OnlineHostInterface,
    is_forced: bool,
    is_dry_run: bool,
    is_caller: bool,
) -> AgentUpgradeOutcome:
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    if not host.is_directory(state_dir):
        return _skipped(agent.id, agent.name, SKIP_DESTROYED, is_dry_run)
    upgrade_to_apply = _build_upgrade_to_apply(agent, upgradable_agent, host, state_dir, is_dry_run)
    if not isinstance(upgrade_to_apply, _UpgradeToApply):
        return upgrade_to_apply
    if upgrade_to_apply.upgraded_files:
        # Ends the run with rsync's install hint.
        RSYNC.require()

    staging_dir = state_dir / UPGRADE_STAGING_DIR_NAME
    with _staging_dir_removed_on_exit(host, state_dir):
        # Write the new files beside the live ones while the agent still runs.
        try:
            changed_paths = _stage_upgraded_files(host, upgrade_to_apply.upgraded_files, staging_dir, state_dir)
        except (MngrError, OSError) as e:
            return _failed(agent, UpgradeFailedStep.PREPARE, e)
        if not changed_paths:
            return AgentCurrent(agent_id=agent.id, agent_name=agent.name, is_dry_run=is_dry_run)
        if is_caller:
            return _skipped(agent.id, agent.name, SKIP_IS_CALLER, is_dry_run)

        prior_state, plan_or_skip = _probe_upgrade_plan(agent, is_forced)
        outcome = _outcome_of_plan(agent, prior_state, plan_or_skip, changed_paths, is_dry_run)
        if isinstance(outcome, AgentUpgradeSkipped):
            return outcome

        is_restart = _is_restarting_plan(outcome.plan)
        # Checked before the stop, so an agent that could not start again is left running.
        if is_restart:
            try:
                host.check_agent_can_start(agent)
            except MngrError as e:
                return _failed(agent, UpgradeFailedStep.START_CHECK, e)
        if is_dry_run:
            return outcome
        outcome_to_apply = _plan_again_and_stop(agent, host, outcome, is_forced) if is_restart else outcome
        if not isinstance(outcome_to_apply, AgentUpgraded):
            return outcome_to_apply
        return _apply_upgrade(agent, upgradable_agent, host, state_dir, upgrade_to_apply, outcome_to_apply)


def _plan_again_and_stop(
    agent: AgentInterface[Any], host: OnlineHostInterface, planned_outcome: AgentUpgraded, is_forced: bool
) -> AgentUpgraded | AgentUpgradeSkipped | AgentUpgradeFailed:
    """Plan ``planned_outcome``'s upgrade again and stop the agent if that plan restarts it, both after any send to the agent has finished and before another can begin."""
    try:
        with agent.hold_message_lock(timeout_seconds=UPGRADE_MESSAGE_LOCK_TIMEOUT_SECONDS):
            outcome = _plan_again(agent, planned_outcome, is_forced)
            if isinstance(outcome, AgentUpgradeSkipped) or not _is_restarting_plan(outcome.plan):
                return outcome
            try:
                host.stop_agents([agent.id])
            except (CleanupFailedGroup, MngrError) as e:
                return _failed(agent, UpgradeFailedStep.STOP, e)
    except MessageLockTimeoutError as e:
        return _failed(agent, UpgradeFailedStep.LOCK, e)
    return outcome


def _apply_upgrade(
    agent: AgentInterface[Any],
    upgradable_agent: UpgradableAgentMixin,
    host: OnlineHostInterface,
    state_dir: Path,
    upgrade_to_apply: _UpgradeToApply,
    # The planned outcome, whose changed paths are staged in the agent's staging dir; an agent it restarts is stopped by now.
    planned_outcome: AgentUpgraded,
) -> AgentUpgradeOutcome:
    """Swap the staged files in and start the agent again if the plan restarts it; an agent destroyed by then is skipped."""
    staging_dir = state_dir / UPGRADE_STAGING_DIR_NAME

    # A destroy takes no host lock, so it can remove the agent while its upgrade runs.
    if not host.path_exists(state_dir / AGENT_DATA_RELATIVE_PATH):
        return _skipped(agent.id, agent.name, SKIP_DESTROYED, planned_outcome.is_dry_run)

    try:
        changed_paths = _restage_if_inputs_changed(
            upgradable_agent, host, state_dir, upgrade_to_apply, planned_outcome.changed_paths
        )
    except (MngrError, OSError) as e:
        return _failed(agent, UpgradeFailedStep.SWAP, e)
    try:
        is_swapped = _swap_in_staged_files(
            host, staging_dir, state_dir, changed_paths, upgrade_to_apply.ready_signal_paths
        )
    except (MngrError, OSError) as e:
        return _failed(agent, UpgradeFailedStep.SWAP, e)
    if not is_swapped:
        return _skipped(agent.id, agent.name, SKIP_DESTROYED, planned_outcome.is_dry_run)

    if _is_restarting_plan(planned_outcome.plan):
        try:
            host.start_agents([agent.id])
        except MngrError as e:
            return _failed(agent, UpgradeFailedStep.START, e)
    return planned_outcome.model_copy_update(
        to_update(planned_outcome.field_ref().changed_paths, tuple(changed_paths))
    )


def _restage_if_inputs_changed(
    upgradable_agent: UpgradableAgentMixin,
    host: OnlineHostInterface,
    state_dir: Path,
    upgrade_to_apply: _UpgradeToApply,
    staged_changed_paths: Sequence[Path],
) -> list[Path]:
    """The paths to swap in: ``staged_changed_paths``, or, when an input changed since the first read, those of the files rebuilt and restaged from it."""
    input_content_by_path = _read_upgrade_inputs(host, state_dir, tuple(upgrade_to_apply.input_content_by_path))
    if input_content_by_path == upgrade_to_apply.input_content_by_path:
        return list(staged_changed_paths)
    with log_span("Rebuilding the upgraded files from inputs that changed since they were read"):
        upgraded_files = upgradable_agent.build_upgraded_files(input_content_by_path)
    return _stage_upgraded_files(host, upgraded_files, state_dir / UPGRADE_STAGING_DIR_NAME, state_dir)


def _build_upgrade_to_apply(
    agent: AgentInterface[Any],
    upgradable_agent: UpgradableAgentMixin,
    host: OnlineHostInterface,
    state_dir: Path,
    is_dry_run: bool,
) -> _UpgradeToApply | AgentUpgradeSkipped | AgentUpgradeFailed:
    """Read what the harness builds from and build it, or return the outcome that ends the agent's upgrade first."""
    try:
        ready_signal_paths = tuple(map(_require_path_inside_state_dir, upgradable_agent.get_ready_signal_paths()))
    except MngrError as e:
        return _failed(agent, UpgradeFailedStep.PREPARE, e)
    try:
        input_content_by_path = _read_upgrade_inputs(host, state_dir, upgradable_agent.get_upgrade_input_paths())
    except (MngrError, OSError) as e:
        return _failed(agent, UpgradeFailedStep.PREPARE, e)
    try:
        unsupported_detail = upgradable_agent.get_upgrade_unsupported_detail(input_content_by_path)
    except MngrError as e:
        return _failed(agent, UpgradeFailedStep.PREPARE, e)
    if unsupported_detail is not None:
        return _skipped(
            agent.id, agent.name, _append_recreate_command(unsupported_detail, str(agent.name)), is_dry_run
        )

    try:
        upgraded_files = upgradable_agent.build_upgraded_files(input_content_by_path)
    except MngrError as e:
        return _failed(agent, UpgradeFailedStep.PREPARE, e)
    return _UpgradeToApply(
        input_content_by_path=input_content_by_path,
        upgraded_files=upgraded_files,
        ready_signal_paths=ready_signal_paths,
    )


@pure
def _append_recreate_command(harness_detail: str, agent_label: str) -> UpgradeSkip:
    """A harness's unsupported detail, then the command that fixes it, such as ``<detail>; re-create it with `mngr create my-agent --reuse --update` ``."""
    return UpgradeSkip(
        kind=UpgradeSkipKind.UNSUPPORTED,
        detail=UpgradeSkipDetail(f"{harness_detail}; re-create it with `mngr create {agent_label} --reuse --update`"),
    )


def _describe_missing_upgrade_support(agent: AgentInterface[Any], mngr_ctx: MngrContext) -> UpgradeSkip:
    """Why an agent whose class has no upgrade support cannot be upgraded: its type has none, or the config defines no such type."""
    if not check_agent_type_known(agent.agent_type, mngr_ctx.config):
        return UpgradeSkip(
            kind=UpgradeSkipKind.UNSUPPORTED,
            detail=UpgradeSkipDetail(f"agent type '{agent.agent_type}' is not defined in this config"),
        )
    return UpgradeSkip(
        kind=UpgradeSkipKind.UNSUPPORTED,
        detail=UpgradeSkipDetail(f"agent type '{agent.agent_type}' has no upgrade support"),
    )


def _probe_upgrade_plan(
    agent: AgentInterface[Any], is_forced: bool
) -> tuple[AgentLifecycleState, UpgradePlan | UpgradeSkip]:
    state = agent.get_lifecycle_state()
    is_blocked_on_dialog = state == AgentLifecycleState.WAITING and agent.is_blocked_on_dialog()
    return state, plan_agent_upgrade(state, is_blocked_on_dialog, is_forced)


def _plan_again(
    agent: AgentInterface[Any], planned_outcome: AgentUpgraded, is_forced: bool
) -> AgentUpgraded | AgentUpgradeSkipped:
    """``planned_outcome``'s upgrade planned again from the agent's state as read now."""
    state, plan_or_skip = _probe_upgrade_plan(agent, is_forced)
    return _outcome_of_plan(agent, state, plan_or_skip, planned_outcome.changed_paths, planned_outcome.is_dry_run)


def _read_upgrade_inputs(
    host: OnlineHostInterface, state_dir: Path, input_paths: Sequence[Path]
) -> dict[Path, str | None]:
    """The content of each of ``input_paths`` under ``state_dir``, None for a missing one."""
    with log_span("Reading {} upgrade input(s) from {}", len(input_paths), state_dir):
        return {input_path: _read_upgrade_input(host, state_dir, input_path) for input_path in input_paths}


def _read_upgrade_input(host: OnlineHostInterface, state_dir: Path, input_path: Path) -> str | None:
    """The text of ``input_path`` under ``state_dir``, None when it is missing."""
    try:
        return host.read_text_file(state_dir / _require_path_inside_state_dir(input_path))
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as e:
        raise InvalidUpgradeInputError(f"{input_path} is not UTF-8 text: {e}") from e


def _stage_upgraded_files(
    host: OnlineHostInterface,
    upgraded_files: Mapping[Path, HarnessFileSpec],
    staging_dir: Path,
    state_dir: Path,
) -> list[Path]:
    """Upload ``upgraded_files`` into ``staging_dir`` and return the relative paths of those whose bytes or executable bit differ from the live ones, sorted."""
    if not upgraded_files:
        return []
    _run_host_command(host, f"rm -rf {shlex.quote(str(staging_dir))}")
    with tempfile.TemporaryDirectory(prefix="mngr-upgrade-") as local_staging_dir_name:
        local_staging_dir = Path(local_staging_dir_name)
        for relative_path, upgraded_file in upgraded_files.items():
            _write_local_upgraded_file(
                local_staging_dir / _require_path_inside_state_dir(relative_path), upgraded_file
            )
        with log_span("Uploading {} upgraded file(s) to {}", len(upgraded_files), staging_dir):
            host.copy_local_directory(local_staging_dir, staging_dir, None)
    changed_paths_output = _run_host_command(
        host,
        f"cd {shlex.quote(str(staging_dir))} && find . -type f | while IFS= read -r path; do "
        f'live={shlex.quote(str(state_dir))}/"$path"; '
        'if cmp -s "$path" "$live" 2>/dev/null '
        '&& { [ -x "$path" ] && [ -x "$live" ] || { [ ! -x "$path" ] && [ ! -x "$live" ]; }; }; '
        'then :; else printf "%s\\n" "$path"; fi; done',
    )
    return sorted(Path(line) for line in changed_paths_output.split("\n") if line)


def _write_local_upgraded_file(path: Path, upgraded_file: HarnessFileSpec) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(upgraded_file.content.encode())
    path.chmod(int(upgraded_file.mode, 8))


@pure
def _require_path_inside_state_dir(relative_path: Path) -> Path:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise _PathOutsideStateDirError(f"{relative_path} is not a path inside the agent's state dir")
    return relative_path


def _swap_in_staged_files(
    host: OnlineHostInterface,
    staging_dir: Path,
    state_dir: Path,
    changed_paths: Sequence[Path],
    ready_signal_paths: Sequence[Path],
) -> bool:
    """Rename each changed staged file over its live path, remove the readiness markers, and rewrite the host-level libs; False, changing nothing, when the agent's record is gone."""
    live_dirs = sorted({str((state_dir / path).parent) for path in changed_paths})
    live_dir_creations = [f"mkdir -p {' '.join(shlex.quote(live_dir) for live_dir in live_dirs)}"] if live_dirs else []
    renames = [
        f"mv -f {shlex.quote(str(staging_dir / path))} {shlex.quote(str(state_dir / path))}" for path in changed_paths
    ]
    # A marker the previous run left would otherwise satisfy the readiness wait before the new files run.
    ready_signal_removals = [f"rm -f {shlex.quote(str(state_dir / path))}" for path in ready_signal_paths]
    swap_commands = [*live_dir_creations, *renames, *ready_signal_removals]
    if swap_commands:
        # Tested in the command that renames, whose mkdir would recreate the state dir of a destroyed agent.
        quoted_record_path = shlex.quote(str(state_dir / AGENT_DATA_RELATIVE_PATH))
        with log_span("Renaming {} upgraded file(s) into {}", len(changed_paths), state_dir):
            swap_output = _run_host_command(
                host,
                f"if [ -e {quoted_record_path} ]; then {' && '.join(swap_commands)}; "
                f"else echo {_RECORD_MISSING_SWAP_OUTPUT}; fi",
            )
        if swap_output.strip() == _RECORD_MISSING_SWAP_OUTPUT:
            return False
    write_shared_shell_libs(host, host.host_dir / "commands")
    return True


def _run_host_command(host: OnlineHostInterface, command: str) -> str:
    result = host.execute_idempotent_command(command, timeout_seconds=_HOST_FILE_COMMAND_TIMEOUT_SECONDS)
    if not result.success:
        logger.debug("Failed host command `{}`: {}", command, result.stderr)
        raise _HostCommandFailedError(result.stderr.strip() or _SILENT_HOST_COMMAND_FAILURE_MESSAGE)
    return result.stdout


@contextmanager
def _staging_dir_removed_on_exit(host: OnlineHostInterface, state_dir: Path) -> Generator[None, None, None]:
    """On exit, remove the staging dir in ``state_dir``, and ``state_dir`` too when that leaves it empty, as after a destroy."""
    try:
        yield
    finally:
        host.execute_idempotent_command(
            f"rm -rf {shlex.quote(str(state_dir / UPGRADE_STAGING_DIR_NAME))}; "
            f"rmdir {shlex.quote(str(state_dir))} 2>/dev/null || true",
            timeout_seconds=_HOST_FILE_COMMAND_TIMEOUT_SECONDS,
        )


def _wait_for_restarted_agent(
    agent: AgentInterface[Any], upgradable_agent: UpgradableAgentMixin, outcome: AgentUpgraded
) -> AgentUpgradeOutcome:
    try:
        upgradable_agent.wait_for_ready_after_restart(timeout=agent.get_ready_timeout_seconds())
    except MngrError as e:
        return _failed(agent, UpgradeFailedStep.READY, e)
    return outcome


@pure
def _is_restarting_plan(plan: UpgradePlan) -> bool:
    match plan:
        case UpgradePlan.RESTART_IDLE | UpgradePlan.RESTART_INTERRUPTING:
            return True
        case UpgradePlan.LEAVE_STOPPED:
            return False
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _is_outcome_void_for_destroyed_agent(outcome: AgentUpgradeOutcome) -> bool:
    """Whether ``outcome`` is one an agent destroyed during the run does not get: any but a completed upgrade."""
    match outcome:
        case AgentUpgraded():
            return outcome.is_dry_run
        case AgentCurrent() | AgentUpgradeSkipped() | AgentUpgradeFailed():
            return True
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _is_agent_stop_attempted(outcome: AgentUpgradeOutcome) -> bool:
    match outcome:
        case AgentUpgraded():
            return not outcome.is_dry_run and _is_restarting_plan(outcome.plan)
        case AgentUpgradeFailed():
            return outcome.step in (UpgradeFailedStep.STOP, UpgradeFailedStep.SWAP, UpgradeFailedStep.START)
        case AgentCurrent() | AgentUpgradeSkipped():
            return False
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _outcome_of_plan(
    agent: AgentInterface[Any],
    prior_state: AgentLifecycleState,
    plan_or_skip: UpgradePlan | UpgradeSkip,
    changed_paths: Sequence[Path],
    is_dry_run: bool,
) -> AgentUpgraded | AgentUpgradeSkipped:
    match plan_or_skip:
        case UpgradePlan():
            return AgentUpgraded(
                agent_id=agent.id,
                agent_name=agent.name,
                plan=plan_or_skip,
                prior_state=prior_state,
                changed_paths=tuple(changed_paths),
                is_dry_run=is_dry_run,
            )
        case UpgradeSkip():
            return _skipped(agent.id, agent.name, plan_or_skip, is_dry_run)
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _skipped(agent_id: AgentId, agent_name: AgentName, skip: UpgradeSkip, is_dry_run: bool) -> AgentUpgradeSkipped:
    return AgentUpgradeSkipped(
        agent_id=agent_id, agent_name=agent_name, kind=skip.kind, detail=skip.detail, is_dry_run=is_dry_run
    )


@pure
def _failed(agent: AgentInterface[Any], step: UpgradeFailedStep, error: BaseException) -> AgentUpgradeFailed:
    return AgentUpgradeFailed(
        agent_id=agent.id, agent_name=agent.name, step=step, error_message=_describe_error(error)
    )


@pure
def _describe_error(error: BaseException) -> str:
    """The error's message; for a cleanup group, each of its failures rather than their count."""
    if isinstance(error, CleanupFailedGroup):
        return "; ".join(f"[{failure.category}] {failure.message}" for failure in error.failures)
    return str(error)
