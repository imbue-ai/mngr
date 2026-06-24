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

import copy
import json
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import Message
from claude_agent_sdk import StreamEvent
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SkipValidation

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.create import create as api_create
from imbue.mngr.api.events import EventsTarget
from imbue.mngr.api.events import read_event_content
from imbue.mngr.api.events import try_build_events_target_for_agent
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import find_one_agent
from imbue.mngr.api.find import resolve_to_started_host_and_running_agent
from imbue.mngr.api.list import list_agents
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.api.providers import get_local_host
from imbue.mngr.config.data_types import EnvVar
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import AgentDetails
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
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import TransferMode
from imbue.mngr.utils.jsonl_warn import split_complete_lines
from imbue.mngr.utils.name_generator import generate_agent_name
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_robinhood._agent_sdk.context import build_sdk_mngr_context
from imbue.mngr_robinhood._agent_sdk.context import open_sdk_concurrency_group
from imbue.mngr_robinhood._agent_sdk.hook_bridge import HookBridge
from imbue.mngr_robinhood._agent_sdk.message_parser import build_result_message
from imbue.mngr_robinhood._agent_sdk.message_parser import build_system_init_message
from imbue.mngr_robinhood._agent_sdk.message_parser import collect_assistant_text
from imbue.mngr_robinhood._agent_sdk.message_parser import parse_transcript_event
from imbue.mngr_robinhood._agent_sdk.pricing import accumulate_usage_totals
from imbue.mngr_robinhood._agent_sdk.pricing import compute_total_cost_usd
from imbue.mngr_robinhood._agent_sdk.stream_events import StreamEventSynthesizer
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
from imbue.mngr_robinhood.errors import AgentSdkNotImplementedError
from imbue.mngr_robinhood.errors import RobinhoodError
from imbue.mngr_robinhood.raw_transcript import RAW_TRANSCRIPT_PATH

# Label attached to every agent the SDK creates, so they are recognizable in ``mngr list`` and
# can be enumerated by the session functions.
SDK_CREATED_BY_LABEL: Final[Mapping[str, str]] = {"created-by": "robinhood-agent-sdk"}

# Agent-type config injected (only) when the caller requests include_partial_messages: enable the
# mngr_claude tmux response-streaming watcher at a 0.25s poll cadence. Unlike the robinhood CLI's
# streaming settings, this deliberately does NOT force the model to sonnet -- the SDK honors the
# caller's requested model.
_SDK_STREAMING_SETTINGS: Final[tuple[str, ...]] = ("agent_types.claude.streaming_snapshot_interval_seconds=0.25",)

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
    hook_bridge: HookBridge | None = Field(
        default=None, description="Local HTTP bridge serving can_use_tool / hooks callbacks, when configured"
    )
    events_target: EventsTarget | None = Field(default=None, description="Where the raw transcript is read")
    seen_bytes: int = Field(default=0, description="Bytes of the raw transcript already consumed")
    latest_session_id: str | None = Field(default=None, description="Most recent claude session id seen")
    latest_model: str | None = Field(default=None, description="Most recent assistant model id seen")
    latest_usage: dict[str, Any] | None = Field(default=None, description="Most recent assistant usage block")
    turn_usage_totals: dict[str, int] = Field(
        default_factory=dict, description="Token usage accumulated across the current turn (for cost)"
    )
    turn_permission_denials: list[dict[str, Any]] = Field(
        default_factory=list, description="Permission denials recorded by the hook bridge this turn"
    )
    is_init_emitted: bool = Field(default=False, description="Whether the system/init message was emitted")
    turn_count: int = Field(default=0, description="Number of user turns delivered so far")
    server_info: dict[str, Any] | None = Field(
        default=None, description="Cached get_server_info() probe result (commands / output style)"
    )


def resolve_cwd(options: ClaudeAgentOptions) -> Path:
    """Resolve the working directory the agent should run in (defaults to the process cwd)."""
    if options.cwd is None:
        return Path.cwd().resolve()
    return Path(options.cwd).resolve()


def start_session(options: ClaudeAgentOptions) -> LiveSession:
    """Build the mngr context + concurrency group for a session (the agent is created lazily)."""
    normalize_credentials_env()
    concurrency_group = open_sdk_concurrency_group()
    # The concurrency group owns subprocesses and must always be exited; if building the rest of
    # the session fails, tear it down before propagating so its processes are not leaked. A
    # success flag (rather than a broad ``except``) keeps the teardown unconditional on every
    # failure path without swallowing or narrowing the propagating exception.
    is_session_built = False
    try:
        # Enable the tmux response-streaming watcher only when the caller asked for partial
        # messages. Unlike the CLI streaming path, the SDK does not force sonnet -- the caller's
        # requested model is honored (the watcher works for any non-fast-mode model).
        streaming_settings = _SDK_STREAMING_SETTINGS if options.include_partial_messages else ()
        mngr_ctx = apply_unattended_settings(build_sdk_mngr_context(concurrency_group), streaming_settings)
        session = LiveSession(
            options=options,
            cwd=resolve_cwd(options),
            mngr_ctx=mngr_ctx,
            concurrency_group=concurrency_group,
        )
        is_session_built = True
        return session
    finally:
        if not is_session_built:
            concurrency_group.__exit__(None, None, None)


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
    # ``setting_sources`` is intentionally NOT translated to ``--setting-sources``: under mngr,
    # claude runs as an interactive agent and the unattended trust/bypass acceptance it needs to
    # boot lives in the project/local settings sources that mngr writes; passing
    # ``--setting-sources=`` (the value the SDK emits for an empty list) makes claude ignore those
    # and hang on the startup trust dialog. Hermeticity from the repo's CLAUDE.md/hooks instead
    # comes from running the agent in an isolated ``cwd`` (see ``resolve_cwd``).
    #
    # ``resume`` / ``continue_conversation`` / ``fork_session`` are deliberately NOT translated to
    # claude flags: each mngr agent has its own claude config dir, so a fresh agent's ``--resume``
    # would not find another agent's session file. Continuation is instead handled by reusing (and
    # restarting) the agent that already owns the session -- see ``deliver_turn``. ``fork_session``
    # is not supported on the mngr transport at all: ``deliver_turn`` raises
    # ``AgentSdkNotImplementedError`` because claude's ``--fork-session`` does not assign a new
    # session id when driven interactively over an adopted, resumed session.
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
    # When can_use_tool / hooks are configured, point claude at the bridge's hooks settings file so
    # its hook commands call back into the in-process callbacks.
    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        name=_build_agent_name(),
        target_path=session.cwd,
        transfer_mode=TransferMode.NONE,
        initial_message=initial_message,
        agent_args=map_options_to_agent_args(session.options) + _bridge_settings_args(session),
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
    host = session.host
    if agent is None or host is None:
        raise TurnDeliveryError("Cannot send a message before the agent is created")
    # The SDK only runs against locally-created Claude agents (see _build_create_options),
    # so the provider is fixed; constructing the AgentMatch directly avoids a discovery
    # round-trip.
    match = AgentMatch(
        agent_id=agent.id,
        agent_name=agent.name,
        host_id=agent.host_id,
        host_name=host.get_name(),
        provider_name=LOCAL_PROVIDER_NAME,
    )
    result = send_message_to_agents(
        mngr_ctx=session.mngr_ctx,
        message_content=prompt,
        agents_to_message=(match,),
        error_behavior=ErrorBehavior.ABORT,
        is_start_desired=False,
    )
    if result.failed_agents:
        errors = "; ".join(f"{name}: {error}" for name, error in result.failed_agents)
        raise TurnDeliveryError(f"Failed to deliver prompt to {agent.name}: {errors}")


def _list_sdk_agent_details(mngr_ctx: MngrContext, cwd: Path) -> list[AgentDetails]:
    """List this SDK's agents (by label) whose working directory is ``cwd``, newest first."""
    result = list_agents(mngr_ctx, is_streaming=False)
    created_by = SDK_CREATED_BY_LABEL["created-by"]
    matching = [
        detail
        for detail in result.agents
        if detail.labels.get("created-by") == created_by and Path(detail.work_dir).resolve() == cwd.resolve()
    ]
    return sorted(matching, key=lambda detail: detail.create_time, reverse=True)


def _agent_transcript_contains_session(mngr_ctx: MngrContext, detail: AgentDetails, session_id: str) -> bool:
    """True if the agent's raw transcript references ``session_id`` (i.e. it owns that session)."""
    events_target = try_build_events_target_for_agent(
        mngr_ctx=mngr_ctx,
        agent_id=detail.id,
        agent_name=str(detail.name),
        host_id=detail.host.id,
        provider_name=LOCAL_PROVIDER_NAME,
    )
    if events_target is None:
        return False
    try:
        content = read_event_content(events_target, RAW_TRANSCRIPT_PATH)
    except MngrError:
        return False
    return session_id in content


def _find_reuse_target(session: LiveSession) -> AgentDetails | None:
    """Find the existing SDK agent to reuse for a ``resume`` / ``continue_conversation`` turn."""
    options = session.options
    if not (options.resume or options.continue_conversation):
        return None
    details = _list_sdk_agent_details(session.mngr_ctx, session.cwd)
    if not details:
        return None
    if options.resume:
        for detail in details:
            if _agent_transcript_contains_session(session.mngr_ctx, detail, options.resume):
                return detail
        return None
    # continue_conversation: reuse the most-recently-created session in this directory.
    return details[0]


def _reuse_agent(session: LiveSession, detail: AgentDetails) -> None:
    """Restart (resuming its claude session) and attach the existing agent to ``session``.

    Baselines ``seen_bytes`` to the current transcript end so the upcoming turn's drain only
    reads the new events (the resumed transcript already contains prior turns' terminal stops).
    """
    host_ref, agent_ref = find_one_agent(detail.address, session.mngr_ctx)
    agent, host = resolve_to_started_host_and_running_agent(
        host_ref, agent_ref, allow_auto_start=True, mngr_ctx=session.mngr_ctx
    )
    if not isinstance(agent, ClaudeAgent):
        raise TurnDeliveryError(f"Cannot reuse non-claude agent {agent.name!r}")
    session.agent = agent
    session.host = host
    events_target = build_events_target(session.mngr_ctx, agent)
    if events_target is None:
        raise TurnDeliveryError(f"Cannot read events for reused agent {agent.name}")
    session.events_target = events_target
    try:
        existing_content = read_event_content(events_target, RAW_TRANSCRIPT_PATH)
    except MngrError:
        existing_content = ""
    session.seen_bytes = len(existing_content.encode("utf-8"))
    if session.options.resume:
        session.latest_session_id = session.options.resume


def deliver_turn(session: LiveSession, prompt: str) -> None:
    """Deliver one user turn.

    On the first turn either reuse the agent that owns the requested ``resume`` /
    ``continue_conversation`` session (restarting it so claude resumes), or create a fresh agent
    with the prompt as its initial message. Subsequent turns message the running agent.

    ``fork_session`` is not supported by the mngr transport: claude's ``--fork-session`` does not
    assign a new session id when driven interactively over an adopted, resumed session (the forked
    turn is written under the source id), so a faithful fork cannot be produced. It is therefore
    real-SDK-only.
    """
    if session.options.fork_session:
        raise AgentSdkNotImplementedError(
            "fork_session is not supported by the mngr-backed Agent SDK transport "
            "(claude --fork-session does not assign a new session id when driven interactively)."
        )
    # Reset the per-turn accumulators so computed cost / denials reflect only this turn.
    session.turn_usage_totals = {}
    session.turn_permission_denials = []
    if session.agent is None:
        reuse_target = _find_reuse_target(session)
        if reuse_target is not None:
            _reuse_agent(session, reuse_target)
            _send_message(session, prompt)
        else:
            _create_agent(session, initial_message=prompt)
    else:
        _restart_agent_if_dead(session)
        _send_message(session, prompt)
    session.turn_count += 1


def _restart_agent_if_dead(session: LiveSession) -> None:
    """Restart-with-resume a stopped agent (e.g. after ``interrupt()``) so the next turn continues."""
    if session.agent is not None and session.agent.get_lifecycle_state() in AGENT_DEAD_STATES:
        restart_agent_with_resume(session)


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
        session.turn_usage_totals = accumulate_usage_totals(session.turn_usage_totals, usage)
    stop_reason = message.get("stop_reason")
    return stop_reason if isinstance(stop_reason, str) else None


MessageSink = Callable[[Message], None]


def _resolve_model(session: LiveSession) -> str:
    """The model id to report: the most recent one seen in the transcript, else the requested one."""
    return session.latest_model or (session.options.model or "")


def _emit_init_if_needed(session: LiveSession, sink: MessageSink) -> None:
    """Emit the synthesized ``system``/``init`` message once per session, with the known metadata."""
    if session.is_init_emitted:
        return
    model = _resolve_model(session)
    sink(
        build_system_init_message(
            session_id=session.latest_session_id or "",
            model=model,
            cwd=str(session.cwd),
            tools=_DEFAULT_REPORTED_TOOLS,
        )
    )
    session.is_init_emitted = True


class _TurnDrainTicker(MutableModel):
    """Per-iteration drainer for one turn: pushes SDK messages to a sink, signals end-of-turn.

    Polled by :func:`poll_for_value`. ``tick`` returns ``True`` once the turn has demonstrably
    ended (terminal ``stop_reason``, agent death -- e.g. after ``interrupt()`` stops the agent --
    or a no-progress safety timeout) and ``None`` otherwise. Each parsed message is pushed to
    :attr:`sink` as it arrives so the async client can yield it immediately (rather than batching
    the whole turn), which is what lets ``interrupt()`` end a turn mid-flight.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: LiveSession = Field(description="The live session whose transcript is being drained")
    sink: SkipValidation[MessageSink] = Field(description="Callback invoked with each parsed message in order")
    messages: list[Message] = Field(default_factory=list, description="SDK messages parsed this turn (for result)")
    last_progress_at: float = Field(
        default_factory=time.monotonic, description="``time.monotonic()`` of the last forward progress"
    )
    synthesizer: StreamEventSynthesizer | None = Field(
        default=None, description="Emits partial-message StreamEvents when include_partial_messages is set"
    )

    def _sink_stream_events(self, events: list[StreamEvent]) -> None:
        if not events:
            return
        _emit_init_if_needed(self.session, self.sink)
        for event in events:
            self.sink(event)

    def _emit_stream_events(self) -> None:
        """Poll the stream_buffer (if streaming) and push any synthesized partial StreamEvents."""
        if self.synthesizer is None:
            return
        self._sink_stream_events(
            self.synthesizer.poll(self.session.latest_session_id or "", _resolve_model(self.session))
        )

    def _finalize_stream_events(self) -> None:
        """Push the held-back final delta plus the closing stream framing (clean completion only)."""
        if self.synthesizer is None:
            return
        self._sink_stream_events(
            self.synthesizer.finalize(self.session.latest_session_id or "", _resolve_model(self.session))
        )

    def tick(self) -> bool | None:
        raw_events = _read_new_raw_events(self.session)
        if raw_events:
            self.last_progress_at = time.monotonic()
        parsed_messages: list[Message] = []
        has_terminal_stop = False
        # Absorb metadata first (this sets latest_session_id) but defer sinking the authoritative
        # transcript messages until after the stream events, so the ordering matches the real SDK:
        # all partial StreamEvents -- including the closing message_stop -- precede the final
        # AssistantMessage.
        for raw_event in raw_events:
            stop_reason = _absorb_event_metadata(self.session, raw_event)
            message = parse_transcript_event(raw_event)
            if message is not None:
                parsed_messages.append(message)
            if stop_reason in TERMINAL_STOP_REASONS:
                has_terminal_stop = True
        self._emit_stream_events()
        if has_terminal_stop:
            self._finalize_stream_events()
        for message in parsed_messages:
            _emit_init_if_needed(self.session, self.sink)
            self.messages.append(message)
            self.sink(message)
        if has_terminal_stop:
            return True
        if self.session.agent is not None and self.session.agent.get_lifecycle_state() in AGENT_DEAD_STATES:
            return True
        if time.monotonic() - self.last_progress_at > TURN_END_NO_PROGRESS_TIMEOUT_SECONDS:
            logger.warning("SDK turn ended on no-progress safety timeout")
            return True
        return None


def drain_turn(session: LiveSession, sink: MessageSink) -> None:
    """Poll the transcript until the turn ends, pushing each message (init, content, result) to ``sink``.

    Emits a synthesized ``system``/``init`` message before the first content message of the session
    and a synthesized terminal ``ResultMessage`` last. End-of-turn is a terminal ``stop_reason``;
    agent death (including an ``interrupt()``-driven stop) and a no-progress safety timeout are the
    fallback exits.
    """
    turn_start = time.monotonic()
    ticker = _TurnDrainTicker(session=session, sink=sink, synthesizer=_build_stream_synthesizer(session))
    # The ticker owns the real stop decision (including its no-progress safety check); the outer
    # timeout is deliberately oversized so it only trips in pathological cases.
    outer_timeout_seconds = TURN_END_NO_PROGRESS_TIMEOUT_SECONDS * 10
    end_of_turn, _, _ = poll_for_value(
        producer=ticker.tick, timeout=outer_timeout_seconds, poll_interval=POLL_INTERVAL_SECONDS
    )
    if end_of_turn is None:
        logger.warning(
            "SDK turn drain hit the outer {:.0f}s timeout without a detected end-of-turn; "
            "finalizing with the {} message(s) accumulated so far",
            outer_timeout_seconds,
            len(ticker.messages),
        )
    # The ticker emits the closing stream framing in the terminal tick (before the final
    # AssistantMessage); an interrupt / timeout leaves the partial sequence deliberately
    # unterminated (mirror the transport), so there is nothing to finalize here.
    # If the turn produced no surfaced content (e.g. interrupted before the first message), still
    # emit the init message before the terminal result so ordering is consistent.
    _emit_init_if_needed(session, sink)
    duration_ms = max(1, int((time.monotonic() - turn_start) * 1000))
    sink(_build_turn_result_message(session, ticker.messages, duration_ms))


def _build_stream_synthesizer(session: LiveSession) -> StreamEventSynthesizer | None:
    """Build the partial-message synthesizer for this turn, or None when streaming is not enabled."""
    if not session.options.include_partial_messages:
        return None
    agent = session.agent
    host = session.host
    if agent is None or host is None:
        return None
    return StreamEventSynthesizer(
        host=host,
        buffer_path=agent.get_live_output_path(),
        reader=agent.make_live_output_reader(),
    )


def _build_turn_result_message(session: LiveSession, turn_messages: Sequence[Message], duration_ms: int) -> Message:
    """Build the synthesized terminal ``ResultMessage`` for a turn from accumulated session state."""
    session_id = session.latest_session_id or ""
    model = _resolve_model(session)
    result_text = collect_assistant_text(turn_messages) or None
    model_usage = {model: session.latest_usage} if (model and session.latest_usage is not None) else None
    # Cost is computed from the turn's accumulated token usage (the session JSONL has no cost field).
    total_cost_usd = compute_total_cost_usd(model, session.turn_usage_totals) if model else None
    return build_result_message(
        session_id=session_id,
        is_error=False,
        result_text=result_text,
        duration_ms=duration_ms,
        duration_api_ms=duration_ms,
        turn_count=session.turn_count,
        usage=session.latest_usage,
        total_cost_usd=total_cost_usd,
        model_usage=model_usage,
        permission_denials=list(session.turn_permission_denials),
        result_uuid=str(uuid4()),
    )


def restart_agent_with_resume(session: LiveSession) -> None:
    """Stop then restart the agent so claude relaunches resuming its session.

    Used by ``interrupt()`` continuation and ``set_model`` / ``set_permission_mode``: the launch
    command re-reads on-disk settings and resumes ``$MAIN_CLAUDE_SESSION_ID``, so a settings
    rewrite before the restart takes effect on the next turn. The raw transcript is append-only and
    reconciled by ``stream_transcript.sh`` across restarts, so ``seen_bytes`` stays valid.
    """
    agent = session.agent
    host = session.host
    if agent is None or host is None:
        raise TurnDeliveryError("Cannot restart the agent before it is created")
    # Best-effort stop before the relaunch: a leftover-resource cleanup failure
    # (surfaced as a CleanupFailedGroup) must not abort the restart, matching the
    # pre-raise behavior where stop_agents' returned failures were discarded here.
    try:
        host.stop_agents([agent.id])
    except CleanupFailedGroup as exc:
        logger.warning("Cleanup left resources behind while stopping agent {} for restart: {}", agent.name, exc)
    host.start_agents([agent.id])
    is_ready = poll_until(
        lambda: agent.get_lifecycle_state() == AgentLifecycleState.WAITING,
        timeout=AGENT_READY_TIMEOUT_SECONDS,
        poll_interval=POLL_INTERVAL_SECONDS,
    )
    if not is_ready:
        logger.warning("Restarted SDK agent {} did not reach WAITING within timeout; proceeding", agent.name)


def interrupt_session(session: LiveSession) -> None:
    """Interrupt the in-flight turn by stopping the agent; the drain loop then finalizes the turn.

    Stopping the agent's tmux process ends the current generation. The running drain ticker observes
    the agent's dead lifecycle state and emits the terminal ``ResultMessage``, so the response stream
    terminates. The client stays connected; the next ``query()`` restarts-with-resume.
    """
    if session.agent is not None and session.host is not None:
        stop_agent(session.agent, session.host)


def _options_with_overrides(
    options: ClaudeAgentOptions, model: str | None, permission_mode: str | None
) -> ClaudeAgentOptions:
    """Return a copy of ``options`` with ``model`` / ``permission_mode`` overridden where provided.

    ``ClaudeAgentOptions`` is an external (non-frozen) dataclass; a shallow copy with the two fields
    reassigned mirrors the requested change without sharing identity with the original options.
    """
    if model is None and permission_mode is None:
        return options
    updated_options = copy.copy(options)
    if model is not None:
        updated_options.model = model
    if permission_mode is not None:
        # The caller passes a plain str (the SDK's set_permission_mode signature); claude validates it.
        updated_options.permission_mode = permission_mode  # ty: ignore[invalid-assignment]
    return updated_options


def _bridge_settings_args(session: LiveSession) -> tuple[str, ...]:
    """The ``--settings`` args pointing claude at the hook bridge, if one is active."""
    if session.hook_bridge is None:
        return ()
    return ("--settings", str(session.hook_bridge.settings_path))


def _rewrite_agent_launch_command(session: LiveSession) -> None:
    """Rebuild the agent's stored launch command from the current options.

    The fully-assembled launch command (including ``--model`` / ``--permission-mode``) is frozen in
    the agent's ``data.json`` at create time and re-run verbatim on restart. To make a mid-session
    ``set_model`` / ``set_permission_mode`` actually take effect, rebuild the command from the
    updated options and overwrite that stored command so the next restart relaunches claude with it.
    """
    agent = session.agent
    host = session.host
    if agent is None or host is None:
        return
    new_command = agent.assemble_command(
        host=host,
        agent_args=map_options_to_agent_args(session.options) + _bridge_settings_args(session),
        command_override=None,
        initial_message=None,
    )
    agent.set_command(new_command)


def reconfigure_session(session: LiveSession, model: str | None, permission_mode: str | None) -> None:
    """Apply a mid-session ``set_model`` / ``set_permission_mode`` to the live agent.

    mngr cannot hot-swap a running claude process's model / permission mode, so the new values are
    recorded on the session options and baked into the agent's stored launch command, then the agent
    is restarted on its resumed session under the new configuration. The transcript is append-only
    and reconciled across restarts, so the next turn is read correctly.
    """
    new_options = _options_with_overrides(session.options, model, permission_mode)
    if new_options is session.options:
        return
    session.options = new_options
    if session.agent is not None:
        _rewrite_agent_launch_command(session)
        restart_agent_with_resume(session)


def stop_session(session: LiveSession) -> None:
    """Stop the agent (leaving its session readable) and release the session's subprocesses."""
    if session.agent is not None and session.host is not None:
        stop_agent(session.agent, session.host)
    session.concurrency_group.__exit__(None, None, None)
