from collections.abc import Callable
from collections.abc import Sequence
from enum import auto
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_call
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.agent_addr import find_agent_by_address
from imbue.mngr.api.agent_addr import find_agents_by_addresses
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import ensure_host_started
from imbue.mngr.api.find import group_agents_by_host
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName


class MissingOuterBehavior(UpperCaseStrEnum):
    """What to do when a targeted agent has no accessible outer host."""

    ABORT = auto()
    WARN = auto()
    IGNORE = auto()


class SkippedAgent(FrozenModel):
    """An agent skipped during ``mngr exec --outer`` because no outer host was accessible.

    Distinct from a runtime failure (which lands in
    ``MultiExecResult.failed_agents``); skipped agents were intentionally
    not attempted because their host has no outer.
    """

    agent_id: AgentId = Field(description="Unique identifier for the skipped agent")
    agent_name: AgentName = Field(description="Human-readable name of the skipped agent")
    host_id: HostId = Field(description="Identifier of the inner host the agent runs on")
    provider_name: ProviderInstanceName = Field(description="Provider instance that owns the inner host")
    reason: str = Field(description="Why this agent was skipped")


class ExecResult(FrozenModel):
    """Result of executing a command on an agent's host."""

    agent_name: str = Field(description="Name of the agent the command was executed on")
    stdout: str = Field(description="Standard output from the command")
    stderr: str = Field(description="Standard error from the command")
    success: bool = Field(description="True if the command succeeded")


class OuterExecResult(FrozenModel):
    """Result of executing a command on a single outer host (one row per unique outer)."""

    outer_host: str = Field(description="Canonical outer-host id: outer:<provider>:<inner_host_id>")
    agents: tuple[str, ...] = Field(description="Names of the input agents whose outer host this row corresponds to")
    stdout: str = Field(description="Standard output from the command")
    stderr: str = Field(description="Standard error from the command")
    success: bool = Field(description="True if the command succeeded")


class MultiExecResult(MutableModel):
    """Result of executing a command on multiple agents."""

    successful_results: list[ExecResult] = Field(
        default_factory=list, description="Results from agents where the command was executed"
    )
    failed_agents: list[tuple[str, str]] = Field(
        default_factory=list,
        description="List of (agent_name, error_message) tuples for agents that could not be reached",
    )
    skipped_agents: list[SkippedAgent] = Field(
        default_factory=list,
        description=(
            "Agents skipped because no outer host was accessible "
            "(populated by 'mngr exec --outer' when --missing-outer != abort)."
        ),
    )
    outer_results: list[OuterExecResult] = Field(
        default_factory=list,
        description=(
            "Per-outer-host results from 'mngr exec --outer'. Each row corresponds to "
            "one unique outer host with the list of input agents that mapped to it."
        ),
    )

    @property
    def is_any_failure(self) -> bool:
        return (
            bool(self.failed_agents)
            or any(not r.success for r in self.successful_results)
            or any(not r.success for r in self.outer_results)
        )


@log_call
def exec_command_on_agent(
    mngr_ctx: MngrContext,
    agent_str: str,
    command: str,
    cwd: str | None = None,
    timeout_seconds: float | None = None,
    is_start_desired: bool = True,
) -> ExecResult:
    """Execute a shell command on the host where an agent runs.

    Supports agent address syntax: NAME[@[HOST][.PROVIDER]].

    Resolves the agent by name, ID, or address, optionally starts it if stopped,
    then executes the command on its host (defaulting to the agent's work_dir).
    """
    agent, host = find_agent_by_address(agent_str, mngr_ctx, "exec", is_start_desired=is_start_desired)

    # Determine working directory: explicit --cwd, or agent's work_dir
    effective_cwd = Path(cwd) if cwd is not None else agent.work_dir

    logger.debug("Executing command on agent {}: {}", agent.name, command)
    prefixed_command = host.build_source_env_prefix(agent) + command
    result = host.execute_stateful_command(
        prefixed_command,
        cwd=effective_cwd,
        timeout_seconds=timeout_seconds,
    )

    return ExecResult(
        agent_name=str(agent.name),
        stdout=result.stdout,
        stderr=result.stderr,
        success=result.success,
    )


def _record_failure(
    result: MultiExecResult,
    agent_name: AgentName,
    error_msg: str,
    on_error: Callable[[str, str], None] | None,
    error_behavior: ErrorBehavior,
) -> bool:
    """Record a failure for an agent and return True if the caller should abort."""
    result.failed_agents.append((str(agent_name), error_msg))
    if on_error is not None:
        on_error(str(agent_name), error_msg)
    return error_behavior == ErrorBehavior.ABORT


def _get_online_host_for_agents(
    host_id_str: str,
    agent_list: Sequence[AgentMatch],
    mngr_ctx: MngrContext,
    is_start_desired: bool,
    result: MultiExecResult,
    on_error: Callable[[str, str], None] | None,
    error_behavior: ErrorBehavior,
) -> OnlineHostInterface | None:
    """Get an online host for a group of agents, starting it if needed.

    Returns the online host, or None if the host could not be reached
    (failures are recorded in result).
    """
    provider_name = agent_list[0].provider_name

    try:
        provider = get_provider_instance(provider_name, mngr_ctx)
        host_interface = provider.get_host(HostId(host_id_str))
    except MngrError as e:
        for match in agent_list:
            is_should_abort = _record_failure(
                result,
                match.agent_name,
                f"Failed to get host for agent {match.agent_name}: {e}",
                on_error,
                error_behavior,
            )
            if is_should_abort:
                return None
        return None

    # Ensure host is online (start if needed)
    try:
        started_host, _was_started = ensure_host_started(
            host_interface, is_start_desired=is_start_desired, provider=provider
        )
        return started_host
    except (MngrError, UserInputError) as e:
        for match in agent_list:
            is_should_abort = _record_failure(
                result,
                match.agent_name,
                f"Failed to start host for agent {match.agent_name}: {e}",
                on_error,
                error_behavior,
            )
            if is_should_abort:
                return None
        return None


def _execute_on_single_agent(
    online_host: OnlineHostInterface,
    match: AgentMatch,
    command: str,
    cwd: str | None,
    timeout_seconds: float | None,
    result: MultiExecResult,
    on_success: Callable[[ExecResult], None] | None,
    on_error: Callable[[str, str], None] | None,
    error_behavior: ErrorBehavior,
) -> bool:
    """Execute a command on a single agent. Returns True if the caller should abort."""
    try:
        # Find the agent on the host to get its work_dir and env prefix
        matched_agent: AgentInterface | None = None
        for agent in online_host.get_agents():
            if agent.id == match.agent_id:
                matched_agent = agent
                break

        if matched_agent is None:
            return _record_failure(
                result, match.agent_name, f"Agent {match.agent_name} not found on host", on_error, error_behavior
            )

        effective_cwd = Path(cwd) if cwd is not None else matched_agent.work_dir
        prefixed_command = online_host.build_source_env_prefix(matched_agent) + command

        with log_span("Executing command on agent {}", match.agent_name):
            cmd_result = online_host.execute_stateful_command(
                prefixed_command,
                cwd=effective_cwd,
                timeout_seconds=timeout_seconds,
            )

        exec_result = ExecResult(
            agent_name=str(match.agent_name),
            stdout=cmd_result.stdout,
            stderr=cmd_result.stderr,
            success=cmd_result.success,
        )
        result.successful_results.append(exec_result)
        if on_success is not None:
            on_success(exec_result)
        return False

    except MngrError as e:
        return _record_failure(
            result,
            match.agent_name,
            f"Failed to execute command on agent {match.agent_name}: {e}",
            on_error,
            error_behavior,
        )


def group_matches_by_outer_host(
    matches: Sequence[AgentMatch],
    mngr_ctx: MngrContext,
) -> tuple[dict[str, list[AgentMatch]], list[AgentMatch], list[tuple[AgentMatch, str]]]:
    """Group agent matches by the *real* outer-host id reported by their provider.

    Returns a 3-tuple:

    - ``by_outer``: ``{outer_host_id: [agent_matches...]}`` for agents whose
      provider has an outer (the value the provider returns from
      ``outer_host_id_for``). Two agents whose providers return the same id
      share the same outer machine, so the command runs once for the group.
    - ``no_outer``: agents whose provider returned ``None`` (no accessible
      outer); the caller decides what to do via ``--missing-outer``.
    - ``provider_errors``: ``(match, error_message)`` for agents whose provider
      could not be loaded or raised ``HostNotFoundError`` while computing
      the outer id.
    """
    by_outer: dict[str, list[AgentMatch]] = {}
    no_outer: list[AgentMatch] = []
    errors: list[tuple[AgentMatch, str]] = []
    for match in matches:
        try:
            provider = get_provider_instance(match.provider_name, mngr_ctx)
        except MngrError as e:
            errors.append((match, f"Failed to load provider for agent {match.agent_name}: {e}"))
            continue
        try:
            outer_id = provider.outer_host_id_for(match.host_id)
        except HostNotFoundError as e:
            errors.append((match, str(e)))
            continue
        if outer_id is None:
            no_outer.append(match)
        else:
            by_outer.setdefault(outer_id, []).append(match)
    return by_outer, no_outer, errors


@log_call
def exec_command_on_outer_hosts(
    mngr_ctx: MngrContext,
    agent_identifiers: Sequence[str],
    command: str,
    is_all: bool,
    cwd: str | None = None,
    timeout_seconds: float | None = None,
    missing_outer: MissingOuterBehavior = MissingOuterBehavior.WARN,
    error_behavior: ErrorBehavior = ErrorBehavior.CONTINUE,
    on_outer_success: Callable[[OuterExecResult], None] | None = None,
    on_skip: Callable[[SkippedAgent], None] | None = None,
    on_error: Callable[[str, str], None] | None = None,
) -> MultiExecResult:
    """Execute a shell command on the *outer host* of the targeted agents.

    Agents are grouped by their *real* outer-host id (returned by the
    provider's ``outer_host_id_for``) so the command runs **once per unique
    outer machine**. For example, three docker containers on the same daemon
    map to one outer (the local box or the SSH daemon host) and produce a
    single result row that lists all three input agents.

    When ``missing_outer`` is:
    - ``ABORT``: raise immediately if any targeted agent has no outer host.
    - ``WARN``: skip those agents, append them to ``result.skipped_agents``,
      and invoke ``on_skip`` for each (typically prints a stderr warning).
    - ``IGNORE``: skip silently (still appended to ``result.skipped_agents``
      for programmatic access; ``on_skip`` is *not* invoked).

    The default cwd is the SSH user's home directory on the outer host.
    """
    result = MultiExecResult()

    matches = find_agents_by_addresses(
        raw_identifiers=list(agent_identifiers),
        filter_all=is_all,
        target_state=None,
        mngr_ctx=mngr_ctx,
    )
    if not matches:
        return result

    by_outer, no_outer, errors = group_matches_by_outer_host(matches, mngr_ctx)

    for match, error_msg in errors:
        is_should_abort = _record_failure(result, match.agent_name, error_msg, on_error, error_behavior)
        if is_should_abort:
            return result

    if no_outer:
        if missing_outer == MissingOuterBehavior.ABORT:
            first = no_outer[0]
            raise UserInputError(f"agent {first.agent_name} has no outer host (provider={first.provider_name})")
        for match in no_outer:
            skipped = SkippedAgent(
                agent_id=match.agent_id,
                agent_name=match.agent_name,
                host_id=match.host_id,
                provider_name=match.provider_name,
                reason="no outer host",
            )
            result.skipped_agents.append(skipped)
            if on_skip is not None and missing_outer == MissingOuterBehavior.WARN:
                on_skip(skipped)

    for outer_id, group in by_outer.items():
        first = group[0]
        # Provider already validated by the grouping pass.
        provider = get_provider_instance(first.provider_name, mngr_ctx)

        with provider.outer_host_for(first.host_id) as outer:
            if outer is None:
                # outer_host_id_for said this provider has an outer but the
                # actual open returned None -- treat as a runtime failure.
                err_msg = f"provider returned an outer id but outer_host_for yielded None for outer {outer_id}"
                for match in group:
                    is_should_abort = _record_failure(result, match.agent_name, err_msg, on_error, error_behavior)
                    if is_should_abort:
                        return result
                continue

            # Default cwd = SSH user's home on the outer (None means the connector's default).
            effective_cwd = Path(cwd) if cwd is not None else None
            try:
                with log_span("Executing command on outer host {}", outer_id):
                    cmd_result = outer.execute_stateful_command(
                        command,
                        cwd=effective_cwd,
                        timeout_seconds=timeout_seconds,
                    )
            except MngrError as e:
                err_msg = f"Failed to execute on outer host {outer_id}: {e}"
                for match in group:
                    is_should_abort = _record_failure(result, match.agent_name, err_msg, on_error, error_behavior)
                    if is_should_abort:
                        return result
                continue

            outer_result = OuterExecResult(
                outer_host=outer_id,
                agents=tuple(str(m.agent_name) for m in group),
                stdout=cmd_result.stdout,
                stderr=cmd_result.stderr,
                success=cmd_result.success,
            )
            result.outer_results.append(outer_result)
            if on_outer_success is not None:
                on_outer_success(outer_result)

    return result


@log_call
def exec_command_on_agents(
    mngr_ctx: MngrContext,
    agent_identifiers: Sequence[str],
    command: str,
    is_all: bool,
    cwd: str | None = None,
    timeout_seconds: float | None = None,
    is_start_desired: bool = True,
    error_behavior: ErrorBehavior = ErrorBehavior.CONTINUE,
    # Optional callback invoked on each successful exec
    on_success: Callable[[ExecResult], None] | None = None,
    # Optional callback invoked on each failure
    on_error: Callable[[str, str], None] | None = None,
) -> MultiExecResult:
    """Execute a shell command on the hosts where multiple agents run.

    Supports agent address syntax: NAME[@[HOST][.PROVIDER]].

    Resolves each agent by name, ID, or address, optionally starts them if stopped,
    then executes the command on each host (defaulting to the agent's work_dir).
    """
    result = MultiExecResult()

    # Find all matching agents (with address support for host/provider filtering)
    matches = find_agents_by_addresses(
        raw_identifiers=list(agent_identifiers),
        filter_all=is_all,
        target_state=None,
        mngr_ctx=mngr_ctx,
    )

    if not matches:
        return result

    # Group by host for efficient iteration
    agents_by_host = group_agents_by_host(matches)

    for host_key, agent_list in agents_by_host.items():
        host_id_str, _ = host_key.split(":", 1)

        # Get an online host (starting it if needed)
        online_host = _get_online_host_for_agents(
            host_id_str, agent_list, mngr_ctx, is_start_desired, result, on_error, error_behavior
        )
        if online_host is None:
            if error_behavior == ErrorBehavior.ABORT and result.failed_agents:
                return result
            continue

        # Execute command on each agent on this host
        for match in agent_list:
            is_should_abort = _execute_on_single_agent(
                online_host, match, command, cwd, timeout_seconds, result, on_success, on_error, error_behavior
            )
            if is_should_abort:
                return result

    return result
