import os
import signal
import time
from collections.abc import Iterator
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final
from typing import IO
from typing import Self

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.create import create as api_create
from imbue.mngr.api.events import EventsTarget
from imbue.mngr.api.events import read_event_content
from imbue.mngr.api.events import try_build_events_target_for_agent
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.cli.common_opts import apply_settings_to_config
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import BaseMngrError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentNameStyle
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import TransferMode
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr.utils.name_generator import generate_agent_name
from imbue.mngr_uncapped_claude.data_types import ArgPartition
from imbue.mngr_uncapped_claude.data_types import ResultMeta
from imbue.mngr_uncapped_claude.input_modes import iter_user_prompts
from imbue.mngr_uncapped_claude.output_modes import StreamingOutputWriter
from imbue.mngr_uncapped_claude.output_modes import monotonic_ms_since

# Settings overrides applied to mngr_ctx so the spawned claude agent runs
# unattended (matches what `headless_claude` already turns on).
_UNATTENDED_SETTINGS: Final[tuple[str, ...]] = (
    "agent_types.claude.auto_dismiss_dialogs=true",
    "agent_types.claude.auto_allow_permissions=true",
)

# Poll cadence for end-of-turn detection plus transcript tailing.
_POLL_INTERVAL_SECONDS: Final[float] = 0.1

# Generous readiness timeout: claude needs time to start, dismiss dialogs,
# and reach the prompt-ready state in a fresh worktree before the first
# message is delivered. mngr's 10-second default is too short here.
_AGENT_READY_TIMEOUT_SECONDS: Final[float] = 120.0

# Filename relative to the agent's events directory holding the common
# transcript stream produced by mngr_claude.
_COMMON_TRANSCRIPT_PATH: Final[str] = "claude/common_transcript/events.jsonl"

EXIT_SUCCESS: Final[int] = 0
EXIT_CLAUDE_ERROR: Final[int] = 1
EXIT_MNGR_ERROR: Final[int] = 2


class _RunState(FrozenModel):
    """Bundle of resources owned by a single ``run()`` invocation.

    Held by the signal handler so it can destroy the agent on Ctrl-C.
    """

    model_config = {"arbitrary_types_allowed": True}

    agent: AgentInterface[Any]
    host: OnlineHostInterface
    writer: StreamingOutputWriter


def run(
    mngr_ctx: MngrContext,
    partition: ArgPartition,
    stdin: IO[str],
    stdout: IO[str],
    is_stdin_a_tty: bool,
) -> int:
    """Drive a single ``mngr uncapped-claude`` invocation end-to-end.

    Returns the integer exit code the caller should pass to ``ctx.exit()``.
    """
    mngr_ctx = _apply_unattended_settings(mngr_ctx)

    try:
        prompts = iter_user_prompts(
            partition.input_format,
            partition.positional_prompt,
            stdin,
            is_stdin_a_tty,
        )
        first_prompt = next(prompts, None)
        if first_prompt is None:
            logger.error("no prompt provided")
            return EXIT_MNGR_ERROR
    except MngrError as exc:
        logger.error("{}", exc)
        return EXIT_MNGR_ERROR

    start_time = time.monotonic()
    state, exit_code = _run_with_agent(mngr_ctx, partition, first_prompt, prompts, stdout, start_time)
    if state is not None:
        _destroy_agent(state.agent, state.host)
    return exit_code


def _apply_unattended_settings(mngr_ctx: MngrContext) -> MngrContext:
    """Inject the claude agent-type config overrides for unattended operation."""
    updated_config = apply_settings_to_config(
        mngr_ctx.config,
        _UNATTENDED_SETTINGS,
        mngr_ctx.config.disabled_plugins,
    )
    return mngr_ctx.model_copy_update(to_update(mngr_ctx.field_ref().config, updated_config))


def _run_with_agent(
    mngr_ctx: MngrContext,
    partition: ArgPartition,
    first_prompt: str,
    remaining_prompts: Iterator[str],
    stdout: IO[str],
    start_time: float,
) -> tuple[_RunState | None, int]:
    """Create the agent, drive all turns, return (state, exit_code)."""
    local_host = _get_local_host(mngr_ctx)
    cwd = Path.cwd().resolve()
    source_location = HostLocation(host=local_host, path=cwd)

    agent_name = _build_agent_name()
    pass_env_vars = _build_pass_env_vars()
    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        name=agent_name,
        target_path=cwd,
        transfer_mode=TransferMode.NONE,
        initial_message=first_prompt,
        agent_args=partition.pass_through_agent_args,
        label_options=AgentLabelOptions(labels={"created-by": "uncapped-claude"}),
        environment=pass_env_vars,
        ready_timeout_seconds=_AGENT_READY_TIMEOUT_SECONDS,
    )

    try:
        result = api_create(
            source_location=source_location,
            target_host=local_host,
            agent_options=options,
            mngr_ctx=mngr_ctx,
            create_work_dir=False,
        )
    except (MngrError, BaseMngrError) as exc:
        logger.error("Failed to create agent: {}", exc)
        return None, EXIT_MNGR_ERROR

    agent = result.agent
    host = result.host

    writer = StreamingOutputWriter(output_format=partition.output_format, session_id=str(agent.id), stdout=stdout)
    state = _RunState(agent=agent, host=host, writer=writer)

    events_target = _build_events_target(mngr_ctx, agent)
    if events_target is None:
        logger.error("Cannot read events for agent {} (no online host or volume)", agent.name)
        return state, EXIT_MNGR_ERROR

    with _DestroyOnSignal(state=state):
        try:
            _wait_for_turn_end(agent, events_target, writer)
            for next_prompt in remaining_prompts:
                _send_user_turn(mngr_ctx, agent, next_prompt)
                _wait_for_turn_end(agent, events_target, writer)
        except (MngrError, BaseMngrError) as exc:
            logger.error("Run failed: {}", exc)
            meta = _build_result_meta(start_time, agent_id=str(agent.id), error_text=str(exc))
            writer.finalize(meta)
            return state, EXIT_MNGR_ERROR

    meta = _build_result_meta(start_time, agent_id=str(agent.id), error_text=None)
    writer.finalize(meta)
    return state, EXIT_SUCCESS


def _get_local_host(mngr_ctx: MngrContext) -> OnlineHostInterface:
    """Return the online local host interface."""
    provider = get_provider_instance(LOCAL_PROVIDER_NAME, mngr_ctx)
    host = provider.get_host(HostName(LOCAL_HOST_NAME))
    if not isinstance(host, OnlineHostInterface):
        raise MngrError("Local host is not online; cannot run uncapped-claude")
    return host


def _build_agent_name() -> AgentName:
    """Auto-generate a name with the ``uncapped-`` prefix."""
    base = generate_agent_name(AgentNameStyle.COOLNAME)
    return AgentName(f"uncapped-{base}")


def _build_pass_env_vars() -> AgentEnvironmentOptions:
    """Forward every variable from the current process environment to the agent."""
    pairs = tuple(EnvVar(key=key, value=value) for key, value in os.environ.items())
    return AgentEnvironmentOptions(env_vars=pairs)


def _build_events_target(mngr_ctx: MngrContext, agent: AgentInterface[Any]) -> EventsTarget | None:
    return try_build_events_target_for_agent(
        mngr_ctx=mngr_ctx,
        agent_id=agent.id,
        agent_name=str(agent.name),
        host_id=agent.host_id,
        provider_name=LOCAL_PROVIDER_NAME,
    )


def _send_user_turn(mngr_ctx: MngrContext, agent: AgentInterface[Any], prompt: str) -> None:
    """Deliver a follow-up prompt to the running agent via ``send_message_to_agents``."""
    include_filter = f'id == "{agent.id}"'
    result = send_message_to_agents(
        mngr_ctx=mngr_ctx,
        message_content=prompt,
        include_filters=(include_filter,),
        exclude_filters=(),
        all_agents=False,
        error_behavior=ErrorBehavior.ABORT,
        is_start_desired=False,
    )
    if result.failed_agents:
        names_and_errors = "; ".join(f"{name}: {error}" for name, error in result.failed_agents)
        raise MngrError(f"Failed to deliver follow-up prompt to {agent.name}: {names_and_errors}")


def _wait_for_turn_end(
    agent: AgentInterface[Any],
    events_target: EventsTarget,
    writer: StreamingOutputWriter,
) -> None:
    """Poll the agent until it reaches WAITING (or a terminal state); stream events meanwhile."""
    seen_chars = 0
    parser_warner = MalformedJsonLineWarner(source_description=f"common transcript for agent {agent.name}")
    is_done = False
    while not is_done:
        seen_chars = _drain_new_events(events_target, writer, parser_warner, seen_chars)
        state = agent.get_lifecycle_state()
        if state in (AgentLifecycleState.WAITING, AgentLifecycleState.STOPPED, AgentLifecycleState.DONE):
            _drain_new_events(events_target, writer, parser_warner, seen_chars)
            is_done = True
        else:
            time.sleep(_POLL_INTERVAL_SECONDS)


def _drain_new_events(
    events_target: EventsTarget,
    writer: StreamingOutputWriter,
    parser_warner: MalformedJsonLineWarner,
    seen_chars: int,
) -> int:
    """Read the transcript file, emit any new events past ``seen_chars``, return new offset."""
    try:
        content = read_event_content(events_target, _COMMON_TRANSCRIPT_PATH)
    except (FileNotFoundError, MngrError):
        return seen_chars
    if len(content) <= seen_chars:
        return seen_chars
    new_lines = content[seen_chars:].splitlines()
    new_events = _parse_event_lines(new_lines, parser_warner)
    if new_events:
        writer.emit_events(new_events)
    return len(content)


def _parse_event_lines(lines: Sequence[str], parser_warner: MalformedJsonLineWarner) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "":
            continue
        parsed = parser_warner.parse(stripped)
        if parsed is None:
            continue
        event, _ = parsed
        events.append(event)
    return events


def _build_result_meta(
    start_time: float,
    agent_id: str,
    error_text: str | None,
) -> ResultMeta:
    return ResultMeta(
        session_id=agent_id,
        duration_ms=monotonic_ms_since(start_time),
        is_error=error_text is not None,
        error_text=error_text,
    )


def _destroy_agent(agent: AgentInterface[Any], host: OnlineHostInterface) -> None:
    """Best-effort: stop and destroy the agent, swallowing cleanup errors."""
    try:
        host.stop_agents([agent.id])
    except (OSError, MngrError, BaseMngrError) as exc:
        logger.warning("Failed to stop agent {}: {}", agent.name, exc)
    try:
        host.destroy_agent(agent)
    except (OSError, MngrError, BaseMngrError) as exc:
        logger.warning("Failed to destroy agent {}: {}", agent.name, exc)


class _DestroyOnSignal(MutableModel):
    """Context manager: traps SIGINT/SIGTERM, destroys the agent, re-raises.

    The handler closes over the state via the instance, so it does not need
    to be defined as a nested function. Original signal handlers are
    restored on exit so the wrapper plays nicely with parents that install
    their own.
    """

    model_config = ConfigDict(frozen=False, extra="forbid", arbitrary_types_allowed=True)

    state: _RunState = Field(description="Run state used by the signal handler")
    original_int: Any = Field(default=None, description="Previous SIGINT handler")
    original_term: Any = Field(default=None, description="Previous SIGTERM handler")

    def __enter__(self) -> Self:
        self.original_int = signal.getsignal(signal.SIGINT)
        self.original_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        return self

    def __exit__(self, *_: object) -> None:
        signal.signal(signal.SIGINT, self.original_int)
        signal.signal(signal.SIGTERM, self.original_term)

    def _on_signal(self, signum: int, _frame: object) -> None:
        logger.warning("Received signal {}; destroying agent {}", signum, self.state.agent.name)
        _destroy_agent(self.state.agent, self.state.host)
        signal.signal(signal.SIGINT, self.original_int)
        signal.signal(signal.SIGTERM, self.original_term)
        os.kill(os.getpid(), signum)
