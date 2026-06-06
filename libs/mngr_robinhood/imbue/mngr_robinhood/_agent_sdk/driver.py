"""Synchronous turn-driver that backs the async Agent SDK with a real mngr claude agent.

One :class:`LiveSession` owns one ``robinhood-`` mngr claude agent. The driver translates the
documented ``ClaudeAgentOptions`` into claude CLI flags (mirroring the real SDK's own
``subprocess_cli`` arg builder), creates the agent through the in-process mngr API, delivers each
user turn, and tails the agent's native session-JSONL transcript -- converting it into
``claude_agent_sdk`` message objects via :mod:`._agent_sdk.message_parser` and detecting
end-of-turn from a terminal ``stop_reason`` (the same signal the robinhood orchestrator uses).

The SDK ``session_id`` is read from the transcript events themselves (each line carries
``sessionId``), never assumed equal to the mngr agent id: claude can rotate its session id over
an agent's lifetime (compaction, ``/clear``, resume, fork), which is why mngr keeps an
append-only ``claude_session_id_history``.
"""

import json
import time
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import Message
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SkipValidation

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.create import create as api_create
from imbue.mngr.api.events import EventsTarget
from imbue.mngr.api.events import read_event_content
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.api.providers import get_local_host
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import AgentEnvironmentOptions
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentNameStyle
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import TransferMode
from imbue.mngr.utils.jsonl_warn import split_complete_lines
from imbue.mngr.utils.name_generator import generate_agent_name
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_robinhood._agent_sdk.context import build_sdk_mngr_context
from imbue.mngr_robinhood._agent_sdk.context import open_sdk_concurrency_group
from imbue.mngr_robinhood._agent_sdk.message_parser import build_result_message
from imbue.mngr_robinhood._agent_sdk.message_parser import build_system_init_message
from imbue.mngr_robinhood._agent_sdk.message_parser import collect_assistant_text
from imbue.mngr_robinhood._agent_sdk.message_parser import parse_transcript_event
from imbue.mngr_robinhood.agent_runtime import AGENT_DEAD_STATES
from imbue.mngr_robinhood.agent_runtime import AGENT_READY_TIMEOUT_SECONDS
from imbue.mngr_robinhood.agent_runtime import POLL_INTERVAL_SECONDS
from imbue.mngr_robinhood.agent_runtime import TERMINAL_STOP_REASONS
from imbue.mngr_robinhood.agent_runtime import TURN_END_NO_PROGRESS_TIMEOUT_SECONDS
from imbue.mngr_robinhood.agent_runtime import apply_unattended_settings
from imbue.mngr_robinhood.agent_runtime import build_events_target
from imbue.mngr_robinhood.agent_runtime import build_pass_env_vars
from imbue.mngr_robinhood.agent_runtime import normalize_credentials_env
from imbue.mngr_robinhood.agent_runtime import stop_agent
from imbue.mngr_robinhood.errors import RobinhoodError
from imbue.mngr_robinhood.raw_transcript import RAW_TRANSCRIPT_PATH

# Label attached to every agent the SDK creates, so they are recognizable in ``mngr list`` and
# can be enumerated by the session functions.
SDK_CREATED_BY_LABEL: Final[Mapping[str, str]] = {"created-by": "robinhood-agent-sdk"}

# The tools list reported in the synthesized ``system``/``init`` message. mngr does not surface
# claude's negotiated tool list at wrapper level, so we report the documented built-in set; this
# is best-effort metadata, not a gate (gating is done by claude via the ``--allowedTools`` /
# ``--disallowedTools`` flags below).
_DEFAULT_REPORTED_TOOLS: Final[tuple[str, ...]] = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
)


class TurnDeliveryError(RobinhoodError):
    """Raised when a turn cannot be delivered to the live agent."""


class LiveSession(MutableModel):
    """All mutable state for one connected ``ClaudeSDKClient`` / ``query()`` session."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    options: SkipValidation[ClaudeAgentOptions] = Field(description="Resolved options for the session")
    cwd: Path = Field(description="Working directory the agent runs in")
    mngr_ctx: MngrContext = Field(description="In-process mngr context")
    concurrency_group: ConcurrencyGroup = Field(description="Owns subprocesses for this session")
    agent: ClaudeAgent | None = Field(default=None, description="The live mngr claude agent, once created")
    host: OnlineHostInterface | None = Field(default=None, description="The agent's host, once created")
    events_target: EventsTarget | None = Field(default=None, description="Where the raw transcript is read")
    seen_bytes: int = Field(default=0, description="Bytes of the raw transcript already consumed")
    latest_session_id: str | None = Field(default=None, description="Most recent claude session id seen")
    latest_model: str | None = Field(default=None, description="Most recent assistant model id seen")
    latest_usage: dict[str, Any] | None = Field(default=None, description="Most recent assistant usage block")
    is_init_emitted: bool = Field(default=False, description="Whether the system/init message was emitted")
    turn_count: int = Field(default=0, description="Number of user turns delivered so far")


def resolve_cwd(options: ClaudeAgentOptions) -> Path:
    """Resolve the working directory the agent should run in (defaults to the process cwd)."""
    if options.cwd is None:
        return Path.cwd().resolve()
    return Path(options.cwd).resolve()


def start_session(options: ClaudeAgentOptions) -> LiveSession:
    """Build the mngr context + concurrency group for a session (the agent is created lazily)."""
    normalize_credentials_env()
    concurrency_group = open_sdk_concurrency_group()
    mngr_ctx = apply_unattended_settings(build_sdk_mngr_context(concurrency_group))
    return LiveSession(
        options=options,
        cwd=resolve_cwd(options),
        mngr_ctx=mngr_ctx,
        concurrency_group=concurrency_group,
    )


def _system_prompt_args(system_prompt: str | Mapping[str, Any] | None) -> list[str]:
    """Translate the documented ``system_prompt`` option into claude CLI flags.

    A bare string replaces the system prompt; a ``{"type": "preset", "append": ...}`` preset
    appends to the default; a preset with no ``append`` (and ``None``) leaves the default alone.
    """
    if isinstance(system_prompt, str):
        return ["--system-prompt", system_prompt]
    if isinstance(system_prompt, Mapping):
        append = system_prompt.get("append")
        if isinstance(append, str):
            return ["--append-system-prompt", append]
    return []


def map_options_to_agent_args(options: ClaudeAgentOptions) -> tuple[str, ...]:
    """Translate the observable ``ClaudeAgentOptions`` subset into claude CLI args.

    Mirrors the real SDK's ``subprocess_cli`` arg builder for the documented, behavior-affecting
    fields. ``cwd`` and ``env`` are applied via mngr create options, not here.
    """
    args: list[str] = []
    args.extend(_system_prompt_args(options.system_prompt))
    if options.allowed_tools:
        args.extend(["--allowedTools", ",".join(options.allowed_tools)])
    if options.disallowed_tools:
        args.extend(["--disallowedTools", ",".join(options.disallowed_tools)])
    if options.model:
        args.extend(["--model", options.model])
    if options.permission_mode:
        args.extend(["--permission-mode", options.permission_mode])
    if options.max_turns:
        args.extend(["--max-turns", str(options.max_turns)])
    for directory in options.add_dirs:
        args.extend(["--add-dir", str(directory)])
    if options.settings:
        args.extend(["--settings", options.settings])
    # ``setting_sources`` is intentionally NOT translated to ``--setting-sources``. Under mngr,
    # claude runs as an interactive agent and mngr writes the unattended trust/bypass acceptance
    # into the project/local settings sources; passing ``--setting-sources=`` (the value the SDK
    # emits for an empty list) would make claude ignore those, leaving it stuck on a trust dialog
    # at startup. Hermeticity from the repo's CLAUDE.md/hooks instead comes from running the
    # agent in an isolated ``cwd`` (see ``resolve_cwd``).
    if options.continue_conversation:
        args.append("--continue")
    if options.resume:
        args.extend(["--resume", options.resume])
    if options.fork_session:
        args.append("--fork-session")
    return tuple(args)


def _build_environment(options: ClaudeAgentOptions) -> AgentEnvironmentOptions:
    """Forward the parent env to the agent, then overlay the options' explicit ``env``."""
    base = build_pass_env_vars()
    if not options.env:
        return base
    overlay = {key: value for key, value in options.env.items()}
    merged = tuple(pair for pair in base.env_vars if pair.key not in overlay)
    extra = tuple(EnvVar(key=key, value=value) for key, value in overlay.items())
    return AgentEnvironmentOptions(env_vars=merged + extra)


def _build_agent_name() -> AgentName:
    """Auto-generate an SDK session agent name with the ``robinhood-`` prefix."""
    base = generate_agent_name(AgentNameStyle.COOLNAME)
    return AgentName(f"robinhood-{base}")


def _create_agent(session: LiveSession, initial_message: str | None) -> None:
    """Create the mngr claude agent for this session (optionally with an initial turn)."""
    local_host = get_local_host(session.mngr_ctx)
    source_location = HostLocation(host=local_host, path=session.cwd)
    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        name=_build_agent_name(),
        target_path=session.cwd,
        transfer_mode=TransferMode.NONE,
        initial_message=initial_message,
        agent_args=map_options_to_agent_args(session.options),
        label_options=AgentLabelOptions(labels=dict(SDK_CREATED_BY_LABEL)),
        environment=_build_environment(session.options),
        ready_timeout_seconds=AGENT_READY_TIMEOUT_SECONDS,
    )
    result = api_create(
        source_location=source_location,
        target_host=local_host,
        agent_options=options,
        mngr_ctx=session.mngr_ctx,
        create_work_dir=False,
    )
    if not isinstance(result.agent, ClaudeAgent):
        stop_agent(result.agent, result.host)
        raise TurnDeliveryError(f"Unexpected agent type from api_create: {type(result.agent).__name__!r}")
    session.agent = result.agent
    session.host = result.host
    events_target = build_events_target(session.mngr_ctx, result.agent)
    if events_target is None:
        raise TurnDeliveryError(f"Cannot read events for agent {result.agent.name}")
    session.events_target = events_target


def _send_message(session: LiveSession, prompt: str) -> None:
    """Deliver a follow-up turn to the already-running agent."""
    agent = session.agent
    if agent is None:
        raise TurnDeliveryError("Cannot send a message before the agent is created")
    result = send_message_to_agents(
        mngr_ctx=session.mngr_ctx,
        message_content=prompt,
        include_filters=(f'id == "{agent.id}"',),
        exclude_filters=(),
        all_agents=False,
        error_behavior=ErrorBehavior.ABORT,
        is_start_desired=False,
    )
    if result.failed_agents:
        errors = "; ".join(f"{name}: {error}" for name, error in result.failed_agents)
        raise TurnDeliveryError(f"Failed to deliver prompt to {agent.name}: {errors}")


def deliver_turn(session: LiveSession, prompt: str) -> None:
    """Deliver one user turn: create the agent with it if first, else message the running agent."""
    if session.agent is None:
        _create_agent(session, initial_message=prompt)
    else:
        _send_message(session, prompt)
    session.turn_count += 1


def _read_new_raw_events(session: LiveSession) -> list[dict[str, Any]]:
    """Read newly-appended raw transcript lines, advance ``seen_bytes``, return parsed dicts."""
    events_target = session.events_target
    if events_target is None:
        return []
    try:
        content = read_event_content(events_target, RAW_TRANSCRIPT_PATH)
    except MngrError as exc:
        # The transcript file does not exist until stream_transcript.sh first writes it.
        if "No such file or directory" not in str(exc):
            logger.warning("Failed to read SDK agent transcript: {}", exc)
        return []
    content_bytes = content.encode("utf-8")
    if len(content_bytes) <= session.seen_bytes:
        return []
    new_slice = content_bytes[session.seen_bytes :].decode("utf-8", errors="replace")
    new_lines, consumed_bytes = split_complete_lines(new_slice)
    if consumed_bytes == 0:
        return []
    session.seen_bytes += consumed_bytes
    raw_events: list[dict[str, Any]] = []
    for line in new_lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            logger.warning("Skipping malformed transcript line: {}", exc)
            continue
        if isinstance(parsed, dict):
            raw_events.append(parsed)
    return raw_events


def _absorb_event_metadata(session: LiveSession, raw_event: Mapping[str, Any]) -> str | None:
    """Update the session's latest session-id / model / usage from a raw event; return stop_reason."""
    session_id = raw_event.get("sessionId")
    if isinstance(session_id, str) and session_id:
        session.latest_session_id = session_id
    if raw_event.get("type") != "assistant":
        return None
    message = raw_event.get("message")
    if not isinstance(message, Mapping):
        return None
    model = message.get("model")
    if isinstance(model, str) and model and model != "<synthetic>":
        session.latest_model = model
    usage = message.get("usage")
    if isinstance(usage, Mapping):
        session.latest_usage = dict(usage)
    stop_reason = message.get("stop_reason")
    return stop_reason if isinstance(stop_reason, str) else None


class _TurnDrainTicker(MutableModel):
    """Per-iteration drainer for one turn: accumulates SDK messages, signals end-of-turn.

    Polled by :func:`poll_for_value`. ``tick`` returns ``True`` once the turn has demonstrably
    ended (terminal ``stop_reason`` past this turn's new transcript bytes, agent death, or a
    no-progress safety timeout) and ``None`` otherwise; the accumulated :attr:`messages` are read
    back by the caller afterward.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: LiveSession = Field(description="The live session whose transcript is being drained")
    messages: list[Message] = Field(default_factory=list, description="SDK messages parsed this turn")
    last_progress_at: float = Field(
        default_factory=time.monotonic, description="``time.monotonic()`` of the last forward progress"
    )

    def tick(self) -> bool | None:
        raw_events = _read_new_raw_events(self.session)
        if raw_events:
            self.last_progress_at = time.monotonic()
        has_terminal_stop = False
        for raw_event in raw_events:
            stop_reason = _absorb_event_metadata(self.session, raw_event)
            message = parse_transcript_event(raw_event)
            if message is not None:
                self.messages.append(message)
            if stop_reason in TERMINAL_STOP_REASONS:
                has_terminal_stop = True
        if has_terminal_stop:
            return True
        if self.session.agent is not None and self.session.agent.get_lifecycle_state() in AGENT_DEAD_STATES:
            return True
        if time.monotonic() - self.last_progress_at > TURN_END_NO_PROGRESS_TIMEOUT_SECONDS:
            logger.warning("SDK turn ended on no-progress safety timeout")
            return True
        return None


def drain_turn(session: LiveSession) -> list[Message]:
    """Poll the transcript until the turn's terminal assistant message arrives; return SDK messages.

    Prepends a synthesized ``system``/``init`` message on the first turn and appends a synthesized
    terminal ``ResultMessage``. End-of-turn is an assistant event with a terminal ``stop_reason``;
    agent death and a no-progress safety timeout are the fallback exits.
    """
    turn_start = time.monotonic()
    ticker = _TurnDrainTicker(session=session)
    # The ticker owns the real stop decision (including its no-progress safety check); the outer
    # timeout is deliberately oversized so it only trips in pathological cases.
    outer_timeout_seconds = TURN_END_NO_PROGRESS_TIMEOUT_SECONDS * 10
    poll_for_value(producer=ticker.tick, timeout=outer_timeout_seconds, poll_interval=POLL_INTERVAL_SECONDS)
    duration_ms = max(1, int((time.monotonic() - turn_start) * 1000))
    return _finalize_turn_messages(session, ticker.messages, duration_ms)


def _finalize_turn_messages(session: LiveSession, turn_messages: Sequence[Message], duration_ms: int) -> list[Message]:
    """Wrap a turn's parsed messages with a leading system/init (first turn) and trailing result."""
    session_id = session.latest_session_id or ""
    model = session.latest_model or (session.options.model or "")
    result_text = collect_assistant_text(turn_messages) or None
    model_usage = {model: session.latest_usage} if (model and session.latest_usage is not None) else None
    result_message = build_result_message(
        session_id=session_id,
        is_error=False,
        result_text=result_text,
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,
        turn_count=session.turn_count,
        usage=session.latest_usage,
        total_cost_usd=None,
        model_usage=model_usage,
        permission_denials=[],
        result_uuid=str(uuid4()),
    )
    finalized: list[Message] = []
    if not session.is_init_emitted:
        finalized.append(
            build_system_init_message(
                session_id=session_id,
                model=model,
                cwd=str(session.cwd),
                tools=_DEFAULT_REPORTED_TOOLS,
            )
        )
        session.is_init_emitted = True
    finalized.extend(turn_messages)
    finalized.append(result_message)
    return finalized


def stop_session(session: LiveSession) -> None:
    """Stop the agent (leaving its session readable) and release the session's subprocesses."""
    if session.agent is not None and session.host is not None:
        stop_agent(session.agent, session.host)
    session.concurrency_group.__exit__(None, None, None)
