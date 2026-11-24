import base64
import binascii
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
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostError
from imbue.mngr.errors import InvalidUpgradeInputError
from imbue.mngr.errors import MessageLockTimeoutError
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.common import check_agent_type_known
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import SHARED_SHELL_LIB_NAMES
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import UpgradableAgentMixin
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import HarnessFileSpec
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import UpgradeSkipDetail
from imbue.mngr.utils.deps import RSYNC
from imbue.mngr.utils.deps import SSH

UPGRADE_STAGING_DIR_NAME: Final[str] = "upgrade_staging"

# Long enough for a send to an idle agent to submit, its Enter retries included.
UPGRADE_MESSAGE_LOCK_TIMEOUT_SECONDS: Final[float] = 45.0

_HOST_FILE_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0

_SILENT_HOST_COMMAND_FAILURE_MESSAGE: Final[str] = "the host command failed without an error message"

# The line a host command prints, in place of its output, when the agent's record is gone.
_RECORD_MISSING_OUTPUT: Final[str] = "agent-record-missing"

# The lines of the input read's output that open each input's base64 content or stand for a missing input.
_INPUT_PRESENT_MARKER: Final[str] = "---MNGR_UPGRADE_INPUT_PRESENT---"
_INPUT_MISSING_MARKER: Final[str] = "---MNGR_UPGRADE_INPUT_MISSING---"

# Makes the upload replace whatever an earlier upload left in the staging dir, even a file of the same size and time.
_STAGING_UPLOAD_RSYNC_ARGS: Final[str] = "--delete -I"

_SHARED_SHELL_LIB_RELATIVE_PATHS: Final[tuple[Path, ...]] = tuple(
    Path("commands") / name for name in SHARED_SHELL_LIB_NAMES
)

# The states of a host that is not online from which `mngr start` brings it back.
_STARTABLE_HOST_STATES: Final[frozenset[HostState]] = frozenset(
    {HostState.STOPPED, HostState.PAUSED, HostState.CRASHED}
)


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
    UNLOCK = auto()
    READY = auto()


class UpgradeSkipKind(UpperCaseStrEnum):
    """What a skip by ``mngr upgrade`` is about, one subject per kind: the agent's state (try again later, or force it; a destroyed agent and one whose host is offline are reported here too, and forcing changes neither), what the agent is (needs a person) or the run (run it again)."""

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
SKIP_DESTROYED: Final[UpgradeSkip] = UpgradeSkip(
    kind=UpgradeSkipKind.NOT_IDLE, detail=UpgradeSkipDetail("it was destroyed during the run")
)
SKIP_ABORTED: Final[UpgradeSkip] = UpgradeSkip(
    kind=UpgradeSkipKind.ABORTED,
    detail=UpgradeSkipDetail("not attempted, because an earlier agent failed"),
)


class UpgradeOutcomeHost(FrozenModel):
    """The host of the agent an outcome of ``mngr upgrade`` is about."""

    id: HostId = Field(description="The host's id")
    name: HostName = Field(description="The host's name")
    provider_name: ProviderInstanceName = Field(description="The provider instance that owns the host")


class AgentUpgraded(FrozenModel):
    """An agent whose files ``mngr upgrade`` rewrote, or would rewrite in a dry run."""

    agent_id: AgentId = Field(description="The agent's id")
    agent_name: AgentName = Field(description="The agent's name")
    host: UpgradeOutcomeHost = Field(description="The agent's host")
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
    host: UpgradeOutcomeHost = Field(description="The agent's host")
    is_dry_run: bool = Field(description="Whether this comes from a dry run, which changes nothing")


class AgentUpgradeSkipped(FrozenModel):
    """An agent ``mngr upgrade`` left out of date and untouched."""

    agent_id: AgentId = Field(description="The agent's id")
    agent_name: AgentName = Field(description="The agent's name")
    host: UpgradeOutcomeHost = Field(description="The agent's host")
    kind: UpgradeSkipKind = Field(description="What the skip is about")
    detail: UpgradeSkipDetail = Field(description="Why the agent was left untouched")
    is_dry_run: bool = Field(description="Whether this comes from a dry run, which changes nothing")


class AgentUpgradeFailed(FrozenModel):
    """An agent for which a step of ``mngr upgrade`` failed."""

    agent_id: AgentId = Field(description="The agent's id")
    agent_name: AgentName = Field(description="The agent's name")
    host: UpgradeOutcomeHost = Field(description="The agent's host")
    step: UpgradeFailedStep = Field(description="The step that failed")
    error_message: str = Field(description="What went wrong")


AgentUpgradeOutcome = AgentUpgraded | AgentCurrent | AgentUpgradeSkipped | AgentUpgradeFailed


class ReachedHost(FrozenModel):
    """The online host of matched agents, with those of them it still holds."""

    host: OnlineHostInterface = Field(description="The host")
    agent_by_id: dict[AgentId, AgentInterface] = Field(description="The matched agents found on the host, by id")


class NotOnlineHost(FrozenModel):
    """The host of matched agents, found not online."""

    state: HostState = Field(description="The host's state")


class UnreachableHost(FrozenModel):
    """The host of matched agents, which could not be looked up or reached."""

    error_message: str = Field(description="What went wrong")


MatchedHost = ReachedHost | NotOnlineHost | UnreachableHost


class ConnectionLostAtUpgradeStepError(HostConnectionError):
    """The connection to an agent's host was lost at the stop, swap or start of its upgrade, which failed the agent there."""

    def __init__(self, failure: AgentUpgradeFailed) -> None:
        self.failure = failure
        super().__init__(failure.error_message)


class _UpgradeToApply(FrozenModel):
    """What ``mngr upgrade`` would change for an agent: the files this mngr builds for it."""

    input_content_by_path: dict[Path, str | None] = Field(
        description="The content of each input the files were built from, None for a missing one"
    )
    upgraded_files: dict[Path, HarnessFileSpec] = Field(description="Every file this mngr generates for the agent")
    ready_signal_paths: tuple[Path, ...] = Field(description="The readiness markers to remove while the agent is down")


class _HostCommandFailedError(MngrError):
    """A host command that ``mngr upgrade`` runs exited with an error, or replied in a form it cannot read."""


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


@pure
def format_agent_label(agent_name: AgentName, host: UpgradeOutcomeHost) -> str:
    """The agent's name on the local host, else its address, such as ``my-agent@my-host.docker``."""
    if host.provider_name == LOCAL_PROVIDER_NAME:
        return str(agent_name)
    return str(AgentAddress(agent=agent_name, host=HostAddress(host=host.name, provider=host.provider_name)))


@pure
def is_upgrade_caller(match: AgentMatch, caller: AgentInstanceKey | None) -> bool:
    """Whether ``match`` is ``caller``, the agent on the local host running this command."""
    return (
        caller is not None
        and match.provider_name == LOCAL_PROVIDER_NAME
        and AgentInstanceKey.build(match.agent_id, match.host_id) == caller
    )


def upgrade_agents(
    matches: Sequence[AgentMatch],
    is_forced: bool,
    is_dry_run: bool,
    error_behavior: ErrorBehavior,
    caller: AgentInstanceKey | None,
    mngr_ctx: MngrContext,
) -> Iterator[AgentUpgradeOutcome]:
    """Upgrade each matched agent in the given order, yielding each outcome; after a failure, ``error_behavior`` says whether the run goes on.

    Each host is reached once, when the first agent named on it comes up, and is never started; once a lost connection
    raises for one of its agents, its later agents are skipped without reaching it again.
    """
    matched_host_by_key: dict[tuple[ProviderInstanceName, HostId], MatchedHost] = {}
    return iter_upgrade_outcomes(
        matches,
        lambda match: _upgrade_named_agent(
            match, matches, matched_host_by_key, is_forced, is_dry_run, caller, mngr_ctx
        ),
        error_behavior,
        is_dry_run,
    )


def _upgrade_named_agent(
    match: AgentMatch,
    # Every agent named, so the first of a host's agents to come up loads them all.
    matches: Sequence[AgentMatch],
    # Each host reached so far: added for its first agent, marked unreachable once its connection is lost.
    matched_host_by_key: dict[tuple[ProviderInstanceName, HostId], MatchedHost],
    is_forced: bool,
    is_dry_run: bool,
    caller: AgentInstanceKey | None,
    mngr_ctx: MngrContext,
) -> AgentUpgradeOutcome:
    host_key = (match.provider_name, match.host_id)
    if host_key not in matched_host_by_key:
        matches_on_host = [other for other in matches if (other.provider_name, other.host_id) == host_key]
        matched_host_by_key[host_key] = reach_matched_host(
            match.provider_name, match.host_id, matches_on_host, mngr_ctx
        )
    is_caller = is_upgrade_caller(match, caller)
    try:
        return upgrade_matched_agent(match, matched_host_by_key[host_key], is_forced, is_dry_run, is_caller, mngr_ctx)
    except ConnectionLostAtUpgradeStepError as e:
        matched_host_by_key[host_key] = UnreachableHost(error_message=str(e))
        return e.failure
    except HostConnectionError as e:
        matched_host_by_key[host_key] = UnreachableHost(error_message=str(e))
        return upgrade_matched_agent(match, matched_host_by_key[host_key], is_forced, is_dry_run, is_caller, mngr_ctx)


def reach_matched_host(
    provider_name: ProviderInstanceName,
    host_id: HostId,
    # The matches on this host, whose agents are loaded from it in one read.
    matches: Sequence[AgentMatch],
    mngr_ctx: MngrContext,
) -> MatchedHost:
    """The host ``host_id`` of ``provider_name`` as found now, with the matched agents it holds when it is online."""
    try:
        provider = get_provider_instance(provider_name, mngr_ctx)
    except MngrError as e:
        return UnreachableHost(error_message=str(e))
    try:
        host = provider.get_host(host_id)
    except MngrError as e:
        return UnreachableHost(error_message=str(e))
    if not isinstance(host, OnlineHostInterface):
        try:
            return NotOnlineHost(state=host.get_state())
        except MngrError as e:
            return UnreachableHost(error_message=str(e))
    agent_refs = [
        DiscoveredAgent(
            host_id=match.host_id, agent_id=match.agent_id, agent_name=match.agent_name, provider_name=provider_name
        )
        for match in matches
    ]
    try:
        agents = host.load_agents_from_refs(agent_refs)
    except MngrError as e:
        return UnreachableHost(error_message=str(e))
    return ReachedHost(host=host, agent_by_id={agent.id: agent for agent in agents})


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
                    yield _skipped(unreached_match, SKIP_ABORTED, is_dry_run)
                return
            case _ as unreachable:
                assert_never(unreachable)


def upgrade_matched_agent(
    match: AgentMatch,
    matched_host: MatchedHost,
    is_forced: bool,
    is_dry_run: bool,
    # Whether the agent is the one running this command.
    is_caller: bool,
    mngr_ctx: MngrContext,
) -> AgentUpgradeOutcome:
    """Upgrade the agent of ``match`` on ``matched_host``, or skip it when the host is offline or no longer holds it.

    A connection to a reached host lost at the host lock, the input read, the staging, the dialog check or the start
    check, all of which leave the agent untouched, raises HostConnectionError; one lost at the stop, swap or start
    raises ConnectionLostAtUpgradeStepError, which carries the agent's failure at that step. One lost while the agent's
    tmux panes and work state are read raises nothing: the state is unknown, so the agent is skipped as unreadable.
    """
    match matched_host:
        case NotOnlineHost():
            agent_label = format_agent_label(match.agent_name, _get_outcome_host(match))
            return _skipped(match, _describe_not_online_host(matched_host.state, agent_label), is_dry_run)
        case UnreachableHost():
            return _skipped(match, _describe_unreachable_host(matched_host.error_message), is_dry_run)
        case ReachedHost():
            agent = matched_host.agent_by_id.get(match.agent_id)
            if agent is None:
                return _skipped(match, SKIP_DESTROYED, is_dry_run)
            return _upgrade_connected_agent(agent, matched_host.host, is_forced, is_dry_run, is_caller, mngr_ctx)
        case _ as unreachable:
            assert_never(unreachable)


def _upgrade_connected_agent(
    agent: AgentInterface[Any],
    host: OnlineHostInterface,
    is_forced: bool,
    is_dry_run: bool,
    is_caller: bool,
    mngr_ctx: MngrContext,
) -> AgentUpgradeOutcome:
    """Rewrite the agent's generated files and restart it if idle (or busy, when forced); a dry run only reports.

    Raises HostConnectionError when the connection to ``host`` is lost at the host lock, the input read, the staging,
    the dialog check or the start check, and ConnectionLostAtUpgradeStepError when it is lost at the stop, swap or
    start; one lost while the agent's tmux panes and work state are read skips the agent as unreadable.
    """
    target = _get_upgrade_target(agent, host)
    if not isinstance(agent, UpgradableAgentMixin):
        return _skipped(target, _describe_missing_upgrade_support(agent, mngr_ctx), is_dry_run)
    outcome = _run_agent_upgrade(target, agent, agent, host, is_forced, is_dry_run, is_caller, mngr_ctx)
    return _skip_if_destroyed(outcome, target, host, is_dry_run)


def _get_upgrade_target(agent: AgentInterface[Any], host: OnlineHostInterface) -> AgentMatch:
    return AgentMatch(
        agent_id=agent.id,
        agent_name=agent.name,
        host_id=host.id,
        host_name=host.get_name(),
        provider_name=host.get_provider_name(),
    )


def _run_agent_upgrade(
    target: AgentMatch,
    agent: AgentInterface[Any],
    # The same agent, as the view that builds its upgraded files.
    upgradable_agent: UpgradableAgentMixin,
    host: OnlineHostInterface,
    is_forced: bool,
    is_dry_run: bool,
    is_caller: bool,
    mngr_ctx: MngrContext,
) -> AgentUpgradeOutcome:
    """Upgrade the agent under the host lock and wait for it after a restart; a lost connection raises as ``_upgrade_connected_agent`` says."""
    outcome = _upgrade_agent_under_host_lock(target, agent, upgradable_agent, host, is_forced, is_dry_run, is_caller)
    if _is_agent_stop_attempted(outcome):
        emit_discovery_events_for_host(mngr_ctx.config, host)
    if isinstance(outcome, AgentUpgraded) and not outcome.is_dry_run and _is_restarting_plan(outcome.plan):
        return _wait_for_restarted_agent(target, agent, upgradable_agent, outcome)
    return outcome


def _upgrade_agent_under_host_lock(
    target: AgentMatch,
    agent: AgentInterface[Any],
    upgradable_agent: UpgradableAgentMixin,
    host: OnlineHostInterface,
    is_forced: bool,
    is_dry_run: bool,
    is_caller: bool,
) -> AgentUpgradeOutcome:
    """The agent's upgrade holding the host lock; before it reaches an outcome, a lost connection propagates and any other host error fails it at the lock step."""
    outcome_holding_lock: AgentUpgradeOutcome | None = None
    try:
        with host.lock_cooperatively(timeout_seconds=START_HOST_LOCK_TIMEOUT_SECONDS):
            outcome_holding_lock = _upgrade_agent_holding_lock(
                target, agent, upgradable_agent, host, is_forced, is_dry_run, is_caller
            )
    except HostError as e:
        if outcome_holding_lock is not None:
            return _get_outcome_of_lost_host_lock(target, outcome_holding_lock, e)
        if isinstance(e, HostConnectionError):
            raise
        return _failed(target, UpgradeFailedStep.LOCK, e)
    return outcome_holding_lock


@pure
def _get_outcome_of_lost_host_lock(
    target: AgentMatch, outcome_holding_lock: AgentUpgradeOutcome, error: HostError
) -> AgentUpgradeOutcome:
    """The outcome when ``error`` ended the host lock after the upgrade reached ``outcome_holding_lock``: a failure at unlock for a real upgrade, else that outcome."""
    if isinstance(outcome_holding_lock, AgentUpgraded) and not outcome_holding_lock.is_dry_run:
        return _failed(target, UpgradeFailedStep.UNLOCK, error)
    return outcome_holding_lock


def _skip_if_destroyed(
    outcome: AgentUpgradeOutcome, target: AgentMatch, host: OnlineHostInterface, is_dry_run: bool
) -> AgentUpgradeOutcome:
    """``outcome``, or the destroyed skip when the agent's record is gone and its files were not rewritten."""
    if not _is_outcome_void_for_destroyed_agent(outcome):
        return outcome
    record_path = get_agent_state_dir_path(host.host_dir, target.agent_id) / AGENT_DATA_RELATIVE_PATH
    try:
        is_record_present = host.path_exists(record_path)
    except HostError as e:
        logger.debug("Could not check whether {} is still there: {}", record_path, e)
        return outcome
    if is_record_present:
        return outcome
    return _skipped(target, SKIP_DESTROYED, is_dry_run)


def _upgrade_agent_holding_lock(
    target: AgentMatch,
    agent: AgentInterface[Any],
    upgradable_agent: UpgradableAgentMixin,
    host: OnlineHostInterface,
    is_forced: bool,
    is_dry_run: bool,
    is_caller: bool,
) -> AgentUpgradeOutcome:
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    upgrade_to_apply = _build_upgrade_to_apply(target, upgradable_agent, host, state_dir, is_dry_run)
    if not isinstance(upgrade_to_apply, _UpgradeToApply):
        return upgrade_to_apply
    if upgrade_to_apply.upgraded_files:
        # Ends the run with rsync's install hint, or with ssh's for a remote host, which rsync reaches over ssh.
        RSYNC.require()
        if not host.is_local:
            SSH.require()

    # Write the new files beside the live ones while the agent still runs.
    staging_dir = state_dir / UPGRADE_STAGING_DIR_NAME
    try:
        changed_paths = _stage_upgraded_files(host, upgrade_to_apply.upgraded_files, staging_dir, state_dir)
    except HostConnectionError:
        _remove_staging_dir(host, state_dir)
        raise
    except (MngrError, OSError) as e:
        _remove_staging_dir(host, state_dir)
        return _failed(target, UpgradeFailedStep.PREPARE, e)
    if changed_paths is None:
        return _skipped(target, SKIP_DESTROYED, is_dry_run)
    if not changed_paths:
        return AgentCurrent(
            agent_id=target.agent_id,
            agent_name=target.agent_name,
            host=_get_outcome_host(target),
            is_dry_run=is_dry_run,
        )

    with _staging_dir_removed_on_exit(host, state_dir):
        if is_caller:
            return _skipped(target, SKIP_IS_CALLER, is_dry_run)
        prior_state, plan_or_skip = _probe_upgrade_plan(agent, is_forced)
        outcome = _outcome_of_plan(target, prior_state, plan_or_skip, changed_paths, is_dry_run)
        if isinstance(outcome, AgentUpgradeSkipped):
            return outcome

        is_restart = _is_restarting_plan(outcome.plan)
        # Checked before the stop, so an agent that could not start again is left running.
        if is_restart:
            try:
                host.check_agent_can_start(agent)
            except HostConnectionError:
                raise
            except MngrError as e:
                return _failed(target, UpgradeFailedStep.START_CHECK, e)
        if is_dry_run:
            return outcome
        outcome_to_apply = _plan_again_and_stop(target, agent, host, outcome, is_forced) if is_restart else outcome
        if not isinstance(outcome_to_apply, AgentUpgraded):
            return outcome_to_apply
        return _apply_upgrade(target, upgradable_agent, host, state_dir, upgrade_to_apply, outcome_to_apply)


def _plan_again_and_stop(
    target: AgentMatch,
    agent: AgentInterface[Any],
    host: OnlineHostInterface,
    planned_outcome: AgentUpgraded,
    is_forced: bool,
) -> AgentUpgraded | AgentUpgradeSkipped | AgentUpgradeFailed:
    """Plan ``planned_outcome``'s upgrade again and stop the agent if that plan restarts it, both after any send to the agent has finished and before another can begin."""
    try:
        with agent.hold_message_lock(timeout_seconds=UPGRADE_MESSAGE_LOCK_TIMEOUT_SECONDS):
            outcome = _plan_again(target, agent, planned_outcome, is_forced)
            if isinstance(outcome, AgentUpgradeSkipped) or not _is_restarting_plan(outcome.plan):
                return outcome
            try:
                host.stop_agents([agent.id])
            except HostConnectionError as e:
                raise ConnectionLostAtUpgradeStepError(_failed(target, UpgradeFailedStep.STOP, e)) from e
            except (CleanupFailedGroup, MngrError) as e:
                return _failed(target, UpgradeFailedStep.STOP, e)
    except MessageLockTimeoutError as e:
        return _failed(target, UpgradeFailedStep.LOCK, e)
    return outcome


def _apply_upgrade(
    target: AgentMatch,
    upgradable_agent: UpgradableAgentMixin,
    host: OnlineHostInterface,
    state_dir: Path,
    upgrade_to_apply: _UpgradeToApply,
    # The planned outcome, whose changed paths are staged in the agent's staging dir; an agent it restarts is stopped by now.
    planned_outcome: AgentUpgraded,
) -> AgentUpgradeOutcome:
    """Swap the staged files in and start the agent again if the plan restarts it; an agent destroyed by then is skipped."""
    staging_dir = state_dir / UPGRADE_STAGING_DIR_NAME
    try:
        changed_paths = _restage_if_inputs_changed(
            upgradable_agent, host, state_dir, upgrade_to_apply, planned_outcome.changed_paths
        )
    except HostConnectionError as e:
        raise ConnectionLostAtUpgradeStepError(_failed(target, UpgradeFailedStep.SWAP, e)) from e
    except (MngrError, OSError) as e:
        return _failed(target, UpgradeFailedStep.SWAP, e)
    if changed_paths is None:
        return _skipped(target, SKIP_DESTROYED, planned_outcome.is_dry_run)
    try:
        is_swapped = _swap_in_staged_files(host, staging_dir, state_dir, changed_paths, upgrade_to_apply)
    except HostConnectionError as e:
        raise ConnectionLostAtUpgradeStepError(_failed(target, UpgradeFailedStep.SWAP, e)) from e
    except MngrError as e:
        return _failed(target, UpgradeFailedStep.SWAP, e)
    if not is_swapped:
        return _skipped(target, SKIP_DESTROYED, planned_outcome.is_dry_run)

    if _is_restarting_plan(planned_outcome.plan):
        try:
            host.start_agents([target.agent_id])
        except HostConnectionError as e:
            raise ConnectionLostAtUpgradeStepError(_failed(target, UpgradeFailedStep.START, e)) from e
        except MngrError as e:
            return _failed(target, UpgradeFailedStep.START, e)
    return planned_outcome.model_copy_update(
        to_update(planned_outcome.field_ref().changed_paths, tuple(changed_paths))
    )


def _restage_if_inputs_changed(
    upgradable_agent: UpgradableAgentMixin,
    host: OnlineHostInterface,
    state_dir: Path,
    upgrade_to_apply: _UpgradeToApply,
    staged_changed_paths: Sequence[Path],
) -> list[Path] | None:
    """The paths to swap in: ``staged_changed_paths``, or, when an input changed since the first read, those of the files rebuilt and restaged from it; None when the agent's record is gone."""
    input_paths = tuple(upgrade_to_apply.input_content_by_path)
    if not input_paths:
        return list(staged_changed_paths)
    input_content_by_path = _read_upgrade_inputs(host, state_dir, input_paths)
    if input_content_by_path is None:
        return None
    if input_content_by_path == upgrade_to_apply.input_content_by_path:
        return list(staged_changed_paths)
    with log_span("Rebuilding the upgraded files from inputs that changed since they were read"):
        upgraded_files = upgradable_agent.build_upgraded_files(input_content_by_path)
    return _stage_upgraded_files(host, upgraded_files, state_dir / UPGRADE_STAGING_DIR_NAME, state_dir)


def _build_upgrade_to_apply(
    target: AgentMatch,
    upgradable_agent: UpgradableAgentMixin,
    host: OnlineHostInterface,
    state_dir: Path,
    is_dry_run: bool,
) -> _UpgradeToApply | AgentUpgradeSkipped | AgentUpgradeFailed:
    """Read what the harness builds from and build it, or return the outcome that ends the agent's upgrade first."""
    try:
        ready_signal_paths = tuple(map(_require_path_inside_state_dir, upgradable_agent.get_ready_signal_paths()))
    except MngrError as e:
        return _failed(target, UpgradeFailedStep.PREPARE, e)
    try:
        input_content_by_path = _read_upgrade_inputs(host, state_dir, upgradable_agent.get_upgrade_input_paths())
    except HostConnectionError:
        raise
    except MngrError as e:
        return _failed(target, UpgradeFailedStep.PREPARE, e)
    if input_content_by_path is None:
        return _skipped(target, SKIP_DESTROYED, is_dry_run)
    try:
        unsupported_detail = upgradable_agent.get_upgrade_unsupported_detail(input_content_by_path)
    except MngrError as e:
        return _failed(target, UpgradeFailedStep.PREPARE, e)
    if unsupported_detail is not None:
        agent_label = format_agent_label(target.agent_name, _get_outcome_host(target))
        return _skipped(target, _append_recreate_command(unsupported_detail, agent_label), is_dry_run)

    try:
        upgraded_files = upgradable_agent.build_upgraded_files(input_content_by_path)
    except MngrError as e:
        return _failed(target, UpgradeFailedStep.PREPARE, e)
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


@pure
def _describe_not_online_host(state: HostState, agent_label: str) -> UpgradeSkip:
    """The skip of an agent whose host is in ``state``, with the command that starts it when `mngr start` can."""
    host_state_clause = f"its host is {state.lower()}"
    if state in _STARTABLE_HOST_STATES:
        return UpgradeSkip(
            kind=UpgradeSkipKind.NOT_IDLE,
            detail=UpgradeSkipDetail(f"{host_state_clause} (`mngr start {agent_label}` starts it)"),
        )
    return UpgradeSkip(kind=UpgradeSkipKind.NOT_IDLE, detail=UpgradeSkipDetail(host_state_clause))


@pure
def _describe_unreachable_host(error_message: str) -> UpgradeSkip:
    return UpgradeSkip(
        kind=UpgradeSkipKind.NOT_IDLE, detail=UpgradeSkipDetail(f"its host could not be reached: {error_message}")
    )


def _probe_upgrade_plan(
    agent: AgentInterface[Any], is_forced: bool
) -> tuple[AgentLifecycleState, UpgradePlan | UpgradeSkip]:
    state = agent.get_lifecycle_state()
    is_blocked_on_dialog = state == AgentLifecycleState.WAITING and agent.is_blocked_on_dialog()
    return state, plan_agent_upgrade(state, is_blocked_on_dialog, is_forced)


def _plan_again(
    target: AgentMatch, agent: AgentInterface[Any], planned_outcome: AgentUpgraded, is_forced: bool
) -> AgentUpgraded | AgentUpgradeSkipped:
    """``planned_outcome``'s upgrade planned again from the agent's state as read now."""
    state, plan_or_skip = _probe_upgrade_plan(agent, is_forced)
    return _outcome_of_plan(target, state, plan_or_skip, planned_outcome.changed_paths, planned_outcome.is_dry_run)


def _read_upgrade_inputs(
    host: OnlineHostInterface, state_dir: Path, input_paths: Sequence[Path]
) -> dict[Path, str | None] | None:
    """The content of each of ``input_paths`` under ``state_dir``, None for a missing one, read in one host command; None when the agent's record is gone."""
    with log_span("Reading {} upgrade input(s) from {}", len(input_paths), state_dir):
        stdout = _run_host_command(host, build_read_upgrade_inputs_command(state_dir, input_paths))
    return parse_upgrade_inputs_output(stdout, input_paths)


@pure
def build_read_upgrade_inputs_command(state_dir: Path, input_paths: Sequence[Path]) -> str:
    """The host command that prints, for each of ``input_paths`` in order, a marker line and then the file's base64 content when it exists.

    It prints only the record-missing line when the agent's record is gone, and fails when an input exists but cannot be read.
    """
    record_check = (
        f"cd {shlex.quote(str(state_dir))} 2>/dev/null && [ -e {shlex.quote(str(AGENT_DATA_RELATIVE_PATH))} ] || "
        f"{{ printf '%s\\n' {shlex.quote(_RECORD_MISSING_OUTPUT)}; exit 0; }}"
    )
    input_reads = [
        f"if [ -e {quoted_path} ]; then printf '%s\\n' {shlex.quote(_INPUT_PRESENT_MARKER)}; "
        f"base64 < {quoted_path} || exit 1; echo; "
        f"else printf '%s\\n' {shlex.quote(_INPUT_MISSING_MARKER)}; fi"
        for quoted_path in (shlex.quote(str(_require_path_inside_state_dir(path))) for path in input_paths)
    ]
    return "\n".join([record_check, *input_reads])


@pure
def parse_upgrade_inputs_output(stdout: str, input_paths: Sequence[Path]) -> dict[Path, str | None] | None:
    """Each of ``input_paths`` with its content from ``build_read_upgrade_inputs_command``'s output, None for a missing one; None when the agent's record is gone."""
    lines = stdout.split("\n")
    if _RECORD_MISSING_OUTPUT in lines:
        return None
    encoded_lines_by_input: list[list[str] | None] = []
    for line in lines:
        if line == _INPUT_PRESENT_MARKER:
            encoded_lines_by_input.append([])
        elif line == _INPUT_MISSING_MARKER:
            encoded_lines_by_input.append(None)
        elif encoded_lines_by_input and (encoded_lines := encoded_lines_by_input[-1]) is not None:
            encoded_lines.append(line)
        else:
            pass
    if len(encoded_lines_by_input) != len(input_paths):
        raise _HostCommandFailedError(
            f"the host reported {len(encoded_lines_by_input)} of the agent's {len(input_paths)} upgrade inputs"
        )
    return {
        input_path: _decode_upgrade_input(input_path, encoded_lines)
        for input_path, encoded_lines in zip(input_paths, encoded_lines_by_input, strict=True)
    }


@pure
def _decode_upgrade_input(input_path: Path, encoded_lines: Sequence[str] | None) -> str | None:
    if encoded_lines is None:
        return None
    try:
        content = base64.b64decode("".join(encoded_lines), validate=True)
    except binascii.Error as e:
        raise _HostCommandFailedError(f"the host sent {input_path} in a form that does not decode: {e}") from e
    try:
        return content.decode()
    except UnicodeDecodeError as e:
        raise InvalidUpgradeInputError(f"{input_path} is not UTF-8 text: {e}") from e


def _stage_upgraded_files(
    host: OnlineHostInterface,
    upgraded_files: Mapping[Path, HarnessFileSpec],
    staging_dir: Path,
    state_dir: Path,
) -> list[Path] | None:
    """Upload ``upgraded_files`` into ``staging_dir`` and return the relative paths of those whose bytes or executable bit differ from the live ones, sorted; None when the agent's record is gone.

    ``staging_dir`` is removed when no file differs or the record is gone.
    """
    if not upgraded_files:
        return []
    with tempfile.TemporaryDirectory(prefix="mngr-upgrade-") as local_staging_dir_name:
        local_staging_dir = Path(local_staging_dir_name)
        for relative_path, upgraded_file in upgraded_files.items():
            _write_local_upgraded_file(
                local_staging_dir / _require_path_inside_state_dir(relative_path), upgraded_file
            )
        with log_span("Uploading {} upgraded file(s) to {}", len(upgraded_files), staging_dir):
            host.copy_local_directory(local_staging_dir, staging_dir, _STAGING_UPLOAD_RSYNC_ARGS)
    changed_paths_output = _run_host_command(host, build_compare_staged_files_command(staging_dir, state_dir))
    changed_path_lines = [line for line in changed_paths_output.split("\n") if line]
    if _RECORD_MISSING_OUTPUT in changed_path_lines:
        return None
    return sorted(Path(line) for line in changed_path_lines)


@pure
def build_compare_staged_files_command(staging_dir: Path, state_dir: Path) -> str:
    """The host command that prints each staged file whose bytes or executable bit differ from the live one, removing ``staging_dir`` when none does.

    When the agent's record is gone it prints only the record-missing line, removing ``staging_dir`` and then ``state_dir`` if that leaves it empty.
    Run again after it removed ``staging_dir``, it prints the same.
    """
    quoted_staging_dir = shlex.quote(str(staging_dir))
    quoted_state_dir = shlex.quote(str(state_dir))
    list_changed_files = (
        "find . -type f | while IFS= read -r path; do "
        f'live={quoted_state_dir}/"$path"; '
        'if cmp -s "$path" "$live" 2>/dev/null '
        '&& { [ -x "$path" ] && [ -x "$live" ] || { [ ! -x "$path" ] && [ ! -x "$live" ]; }; }; '
        'then :; else printf "%s\\n" "$path"; fi; done'
    )
    return (
        f"if [ ! -e {shlex.quote(str(state_dir / AGENT_DATA_RELATIVE_PATH))} ]; then "
        f"rm -rf {quoted_staging_dir}; rmdir {quoted_state_dir} 2>/dev/null; printf '%s\\n' {shlex.quote(_RECORD_MISSING_OUTPUT)}; "
        f"elif [ -d {quoted_staging_dir} ]; then cd {quoted_staging_dir} && changed=$({list_changed_files}) && "
        f'if [ -n "$changed" ]; then printf \'%s\\n\' "$changed"; else rm -rf {quoted_staging_dir}; fi; fi'
    )


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
    upgrade_to_apply: _UpgradeToApply,
) -> bool:
    """Move the changed staged files in, remove the readiness markers, and refresh the host's shared shell libs when the agent ships them; False, changing nothing, when the agent's record is gone."""
    is_shared_lib_shipped = all(
        relative_path in upgrade_to_apply.upgraded_files for relative_path in _SHARED_SHELL_LIB_RELATIVE_PATHS
    )
    swap_command = build_swap_command(
        staging_dir,
        state_dir,
        changed_paths,
        upgrade_to_apply.ready_signal_paths,
        host.host_dir / "commands" if is_shared_lib_shipped else None,
    )
    if swap_command is None:
        return True
    with log_span("Moving {} upgraded file(s) into {}", len(changed_paths), state_dir):
        swap_output = _run_host_command(host, swap_command)
    return swap_output.strip() != _RECORD_MISSING_OUTPUT


@pure
def build_swap_command(
    staging_dir: Path,
    state_dir: Path,
    changed_paths: Sequence[Path],
    ready_signal_paths: Sequence[Path],
    # Where the agent's shared shell libs are copied once its files are in place; None to leave them.
    shared_lib_target_dir: Path | None,
) -> str | None:
    """The host command that moves each changed staged file over its live one, removes the readiness markers and copies the shared shell libs; None when it has nothing to do.

    When the agent's record is gone it changes nothing and prints only the record-missing line.
    Safe to run again after it ran: a move whose staged file is gone and whose live file exists counts as done.
    """
    live_dirs = sorted({str((state_dir / path).parent) for path in changed_paths})
    live_dir_creations = [f"mkdir -p {' '.join(shlex.quote(live_dir) for live_dir in live_dirs)}"] if live_dirs else []
    moves = [
        _build_replayable_move_command(shlex.quote(str(staging_dir / path)), shlex.quote(str(state_dir / path)))
        for path in changed_paths
    ]
    # A marker the previous run left would otherwise satisfy the readiness wait before the new files run.
    ready_signal_removals = [f"rm -f {shlex.quote(str(state_dir / path))}" for path in ready_signal_paths]
    shared_lib_copies = (
        [
            f"mkdir -p {shlex.quote(str(shared_lib_target_dir))}",
            *(
                _build_atomic_copy_command(state_dir / "commands" / name, shared_lib_target_dir / name)
                for name in SHARED_SHELL_LIB_NAMES
            ),
        ]
        if shared_lib_target_dir is not None
        else []
    )
    swap_steps = [*live_dir_creations, *moves, *ready_signal_removals, *shared_lib_copies]
    if not swap_steps:
        return None
    # Tested in the command that moves, whose mkdir would recreate the state dir of a destroyed agent.
    quoted_record_path = shlex.quote(str(state_dir / AGENT_DATA_RELATIVE_PATH))
    return (
        f"if [ -e {quoted_record_path} ]; then {' && '.join(swap_steps)}; "
        f"else printf '%s\\n' {shlex.quote(_RECORD_MISSING_OUTPUT)}; fi"
    )


@pure
def _build_replayable_move_command(quoted_source: str, quoted_target: str) -> str:
    return f"{{ mv -f {quoted_source} {quoted_target} || {{ [ ! -e {quoted_source} ] && [ -e {quoted_target} ]; }}; }}"


@pure
def _build_atomic_copy_command(source: Path, target: Path) -> str:
    """Copy ``source`` to an executable ``target`` through a sibling temp file, so a reader never sees it half written."""
    quoted_temp = shlex.quote(str(target.parent / f".{target.name}.upgrade-tmp"))
    return (
        f"cp {shlex.quote(str(source))} {quoted_temp} && chmod 755 {quoted_temp} && "
        f"mv -f {quoted_temp} {shlex.quote(str(target))}"
    )


def _run_host_command(host: OnlineHostInterface, command: str) -> str:
    result = host.execute_idempotent_command(command, timeout_seconds=_HOST_FILE_COMMAND_TIMEOUT_SECONDS)
    if not result.success:
        logger.debug("Failed host command `{}`: {}", command, result.stderr)
        raise _HostCommandFailedError(result.stderr.strip() or _SILENT_HOST_COMMAND_FAILURE_MESSAGE)
    return result.stdout


@contextmanager
def _staging_dir_removed_on_exit(host: OnlineHostInterface, state_dir: Path) -> Generator[None, None, None]:
    try:
        yield
    finally:
        _remove_staging_dir(host, state_dir)


def _remove_staging_dir(host: OnlineHostInterface, state_dir: Path) -> None:
    """Remove the staging dir in ``state_dir``, and ``state_dir`` too when that leaves it empty, as after a destroy; a host that cannot be reached keeps it for the next upload to replace."""
    try:
        host.execute_idempotent_command(
            f"rm -rf {shlex.quote(str(state_dir / UPGRADE_STAGING_DIR_NAME))}; "
            f"rmdir {shlex.quote(str(state_dir))} 2>/dev/null || true",
            timeout_seconds=_HOST_FILE_COMMAND_TIMEOUT_SECONDS,
        )
    except HostError as e:
        logger.debug("Left the upgrade staging dir in {} in place: {}", state_dir, e)


def _wait_for_restarted_agent(
    target: AgentMatch, agent: AgentInterface[Any], upgradable_agent: UpgradableAgentMixin, outcome: AgentUpgraded
) -> AgentUpgradeOutcome:
    try:
        upgradable_agent.wait_for_ready_after_restart(timeout=agent.get_ready_timeout_seconds())
    except MngrError as e:
        return _failed(target, UpgradeFailedStep.READY, e)
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
    """Whether ``outcome`` is one an agent destroyed during the run does not get, and no host command already found its record for: any but a completed upgrade or a current agent."""
    match outcome:
        case AgentUpgraded():
            return outcome.is_dry_run
        case AgentCurrent():
            return False
        case AgentUpgradeSkipped() | AgentUpgradeFailed():
            return True
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _is_agent_stop_attempted(outcome: AgentUpgradeOutcome) -> bool:
    match outcome:
        case AgentUpgraded():
            return not outcome.is_dry_run and _is_restarting_plan(outcome.plan)
        case AgentUpgradeFailed():
            return outcome.step in (
                UpgradeFailedStep.STOP,
                UpgradeFailedStep.SWAP,
                UpgradeFailedStep.START,
                UpgradeFailedStep.UNLOCK,
            )
        case AgentCurrent() | AgentUpgradeSkipped():
            return False
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _outcome_of_plan(
    target: AgentMatch,
    prior_state: AgentLifecycleState,
    plan_or_skip: UpgradePlan | UpgradeSkip,
    changed_paths: Sequence[Path],
    is_dry_run: bool,
) -> AgentUpgraded | AgentUpgradeSkipped:
    match plan_or_skip:
        case UpgradePlan():
            return AgentUpgraded(
                agent_id=target.agent_id,
                agent_name=target.agent_name,
                host=_get_outcome_host(target),
                plan=plan_or_skip,
                prior_state=prior_state,
                changed_paths=tuple(changed_paths),
                is_dry_run=is_dry_run,
            )
        case UpgradeSkip():
            return _skipped(target, plan_or_skip, is_dry_run)
        case _ as unreachable:
            assert_never(unreachable)


@pure
def _get_outcome_host(target: AgentMatch) -> UpgradeOutcomeHost:
    return UpgradeOutcomeHost(id=target.host_id, name=target.host_name, provider_name=target.provider_name)


@pure
def _skipped(target: AgentMatch, skip: UpgradeSkip, is_dry_run: bool) -> AgentUpgradeSkipped:
    return AgentUpgradeSkipped(
        agent_id=target.agent_id,
        agent_name=target.agent_name,
        host=_get_outcome_host(target),
        kind=skip.kind,
        detail=skip.detail,
        is_dry_run=is_dry_run,
    )


@pure
def _failed(target: AgentMatch, step: UpgradeFailedStep, error: BaseException) -> AgentUpgradeFailed:
    return AgentUpgradeFailed(
        agent_id=target.agent_id,
        agent_name=target.agent_name,
        host=_get_outcome_host(target),
        step=step,
        error_message=_describe_error(error),
    )


@pure
def _describe_error(error: BaseException) -> str:
    """The error's message; for a cleanup group, each of its failures rather than their count."""
    if isinstance(error, CleanupFailedGroup):
        return "; ".join(f"[{failure.category}] {failure.message}" for failure in error.failures)
    return str(error)
