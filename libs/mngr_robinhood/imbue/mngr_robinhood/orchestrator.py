import os
import signal
import time
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from typing import Final
from typing import IO
from typing import Self

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.api.create import create as api_create
from imbue.mngr.api.events import EventsTarget
from imbue.mngr.api.events import read_event_content
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.message import send_message_to_agents
from imbue.mngr.api.providers import get_local_host
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import AgentLabelOptions
from imbue.mngr.interfaces.host import AgentTmuxOptions
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.live_output import LiveOutputReader
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
from imbue.mngr.utils.jsonl_warn import split_complete_lines
from imbue.mngr.utils.name_generator import generate_agent_name
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_robinhood.agent_runtime import AGENT_DEAD_STATES
from imbue.mngr_robinhood.agent_runtime import AGENT_READY_TIMEOUT_SECONDS
from imbue.mngr_robinhood.agent_runtime import POLL_INTERVAL_SECONDS
from imbue.mngr_robinhood.agent_runtime import TERMINAL_STOP_REASONS
from imbue.mngr_robinhood.agent_runtime import TURN_END_NO_PROGRESS_TIMEOUT_SECONDS
from imbue.mngr_robinhood.agent_runtime import apply_unattended_settings
from imbue.mngr_robinhood.agent_runtime import build_events_target
from imbue.mngr_robinhood.agent_runtime import build_pass_env_vars
from imbue.mngr_robinhood.agent_runtime import destroy_agent
from imbue.mngr_robinhood.agent_runtime import normalize_credentials_env
from imbue.mngr_robinhood.data_types import ArgPartition
from imbue.mngr_robinhood.data_types import ResultMeta
from imbue.mngr_robinhood.input_modes import iter_user_prompts
from imbue.mngr_robinhood.output_modes import StreamingOutputWriter
from imbue.mngr_robinhood.output_modes import monotonic_ms_since
from imbue.mngr_robinhood.raw_transcript import RAW_TRANSCRIPT_PATH
from imbue.mngr_robinhood.raw_transcript import RawTranscriptParser

# Extra settings applied when the caller requests live streaming (via
# --include-partial-messages or --stream-plain-text). These enable the
# tmux-based response-streaming watcher on the spawned claude agent, default the
# model to sonnet (so fast mode is off and streaming is observable -- a
# user-passed --model still overrides this default), and force fast mode off.
# They are merged into the SAME apply_settings_to_config call as the unattended
# settings so the settings_overrides dict is assembled in one shot (a second
# merge over the non-empty dict would trip the settings-narrowing guard).
_STREAMING_SETTINGS: Final[tuple[str, ...]] = (
    "agent_types.claude.streaming_snapshot_interval_seconds=0.25",
    "agent_types.claude.settings_overrides.model=sonnet",
    "agent_types.claude.settings_overrides.fastMode=false",
)

EXIT_SUCCESS: Final[int] = 0
EXIT_CLAUDE_ERROR: Final[int] = 1
EXIT_MNGR_ERROR: Final[int] = 2


class _RunState(FrozenModel):
    """Bundle of resources owned by a single ``run()`` invocation.

    Used both by the signal handler (to destroy the agent on Ctrl-C) and
    by ``run()`` itself (to destroy the agent during normal end-of-run
    cleanup).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    agent: ClaudeAgent
    host: OnlineHostInterface
    writer: StreamingOutputWriter


class _TranscriptReadFailureWarner(MutableModel):
    """Emit at most one warning per run for non-ENOENT transcript-read failures.

    ``_drain_new_events`` is called every ``POLL_INTERVAL_SECONDS`` (~100ms).
    If the read fails for a persistent reason other than "file not yet
    created" (e.g. permission denied, host unreachable), logging on every
    poll would flood stderr with hundreds of identical warnings per minute.
    This warner emits the first such failure at WARNING level and silently
    drops subsequent ones (still surfacing them at TRACE for debugging).
    """

    has_warned: bool = Field(default=False, description="Whether the WARNING-level message was already emitted")

    def warn(self, exc: BaseException) -> None:
        if self.has_warned:
            logger.trace("Failed to read common transcript (warning already emitted): {}", exc)
            return
        logger.warning("Failed to read common transcript: {}", exc)
        self.has_warned = True


class _StreamBufferConsumer(MutableModel):
    """Polls the agent's stream_buffer and emits incremental assistant-text deltas.

    Reads the full snapshot each tick and hands it to the agent's
    :class:`LiveOutputReader`, which extracts the new delta (holding back the
    still-streaming final line, whose rendering churns as it grows). ``poll()``
    emits that delta; ``flush()`` is called at turn end to deliver the withheld
    final line and reset the reader for the next turn. Best-effort previews --
    the authoritative assistant message still arrives via the transcript path.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    host: OnlineHostInterface = Field(description="Host to read the buffer file from")
    buffer_path: Path = Field(description="Absolute path to the agent's stream_buffer file")
    writer: StreamingOutputWriter = Field(description="Writer that renders the deltas")
    reader: LiveOutputReader = Field(description="Extracts text deltas from successive buffer snapshots")

    def poll(self) -> None:
        try:
            content = self.host.read_text_file(self.buffer_path)
        except (FileNotFoundError, OSError, MngrError):
            # The buffer may not exist yet (watcher still starting up); benign.
            return
        for delta in self.reader.feed(content):
            self.writer.emit_partial_text(delta)

    def flush(self) -> None:
        """Emit the held-back final line and reset the reader for the next turn.

        Called at turn end (the watcher empties the buffer when the agent goes
        idle), so the final line -- never emitted during streaming because it was
        the volatile last line -- is delivered exactly once. The reset is what
        keeps the next turn (an independent message) from having a shared leading
        prefix stripped by the reader's divergence path and emitted truncated.
        """
        for delta in self.reader.finalize():
            self.writer.emit_partial_text(delta)


def run(
    mngr_ctx: MngrContext,
    partition: ArgPartition,
    stdin: IO[str],
    stdout: IO[str],
    is_stdin_a_tty: bool,
) -> int:
    """Drive a single ``mngr robinhood`` invocation end-to-end.

    Returns the integer exit code the caller should pass to ``ctx.exit()``.
    """
    normalize_credentials_env()
    is_streaming_requested = partition.include_partial_messages or partition.stream_plain_text
    mngr_ctx = apply_unattended_settings(mngr_ctx, _STREAMING_SETTINGS if is_streaming_requested else ())

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
    # ``state_holder`` is populated by ``_run_with_agent`` as soon as the agent
    # has been created. The ``finally`` below destroys whatever ended up in
    # it, so even an unexpected exception inside ``_run_with_agent`` (anything
    # other than the ``MngrError`` it handles internally) cannot leak the
    # agent. On SIGINT the ``_DestroyOnSignal`` handler destroys the agent and
    # then ``os.kill``s the process, so this ``finally`` does not run -- which
    # is intentional and avoids a double-destroy.
    state_holder: list[_RunState] = []
    try:
        exit_code = _run_with_agent(mngr_ctx, partition, first_prompt, prompts, stdout, start_time, state_holder)
    finally:
        if state_holder:
            destroy_agent(state_holder[0].agent, state_holder[0].host)
    return exit_code


def _run_with_agent(
    mngr_ctx: MngrContext,
    partition: ArgPartition,
    first_prompt: str,
    remaining_prompts: Iterator[str],
    stdout: IO[str],
    start_time: float,
    state_holder_out: list[_RunState],
) -> int:
    """Create the agent, drive all turns, return the exit code.

    ``state_holder_out`` is appended-to as soon as the agent has been created;
    the caller owns destroying whatever ends up in it. This split means that
    even an unexpected exception inside this function (anything other than the
    ``MngrError`` cases handled internally) cannot leak the agent.
    """
    local_host = get_local_host(mngr_ctx)
    cwd = Path.cwd().resolve()
    source_location = HostLocation(host=local_host, path=cwd)

    agent_name = _build_agent_name()
    pass_env_vars = build_pass_env_vars()
    options = CreateAgentOptions(
        agent_type=AgentTypeName("claude"),
        name=agent_name,
        target_path=cwd,
        transfer_mode=TransferMode.NONE,
        initial_message=first_prompt,
        agent_args=partition.pass_through_agent_args,
        label_options=AgentLabelOptions(labels={"created-by": "robinhood"}),
        environment=pass_env_vars,
        ready_timeout_seconds=AGENT_READY_TIMEOUT_SECONDS,
        tmux=AgentTmuxOptions(
            width=partition.tmux_width,
            height=partition.tmux_height,
            window_size=partition.tmux_window_size,
        ),
    )

    try:
        result = api_create(
            source_location=source_location,
            target_host=local_host,
            agent_options=options,
            mngr_ctx=mngr_ctx,
            create_work_dir=False,
        )
    except MngrError as exc:
        logger.error("Failed to create agent: {}", exc)
        return EXIT_MNGR_ERROR

    if not isinstance(result.agent, ClaudeAgent):
        # ``api_create`` with ``agent_type=AgentTypeName("claude")`` always
        # returns a ``ClaudeAgent``; this branch is purely a type-narrowing
        # check that should be unreachable in practice. The narrowing is
        # required because ``_RunState.agent`` is typed as ``ClaudeAgent``,
        # and pydantic re-validates field values on model construction --
        # passing the abstract ``AgentInterface`` base would be rejected.
        # Destroy the just-created agent before returning so the unexpected-
        # type path does not leak a live agent on the host.
        logger.error("Unexpected agent type from api_create: {!r}", type(result.agent).__name__)
        destroy_agent(result.agent, result.host)
        return EXIT_MNGR_ERROR
    agent = result.agent
    host = result.host

    writer = StreamingOutputWriter(
        output_format=partition.output_format,
        session_id=str(agent.id),
        stdout=stdout,
        replay_user_messages=partition.replay_user_messages,
        stream_plain_text=partition.stream_plain_text,
    )

    # When streaming is requested, consume the agent's stream_buffer so we can
    # surface incremental assistant-text deltas as they are produced.
    stream_consumer: _StreamBufferConsumer | None = None
    if partition.include_partial_messages or partition.stream_plain_text:
        stream_consumer = _StreamBufferConsumer(
            host=host,
            buffer_path=agent.get_live_output_path(),
            writer=writer,
            reader=agent.make_live_output_reader(),
        )

    state = _RunState(agent=agent, host=host, writer=writer)
    # Publish the state to the caller's holder before any failable work so
    # that the caller's ``finally`` clause can destroy the agent if anything
    # below raises an unexpected exception.
    state_holder_out.append(state)

    events_target = build_events_target(mngr_ctx, agent)
    if events_target is None:
        error_text = f"Cannot read events for agent {agent.name} (no online host or volume)"
        logger.error("{}", error_text)
        _finalize_run(writer, start_time, agent_id=str(agent.id), error_text=error_text, turn_count=1)
        return EXIT_MNGR_ERROR

    final_state: AgentLifecycleState
    # Count conversational turns delivered. The initial_message in
    # CreateAgentOptions counts as the first turn; each follow-up prompt
    # delivered via _send_user_turn adds one more. This drives the
    # turn-count field in claude's native result envelope.
    turn_count = 1
    # Transcript read state is owned by the run, not the per-turn helper, so
    # that multi-turn invocations do not re-read or re-parse lines from prior
    # turns, the malformed-line warner keeps its "warn-once" memory across
    # turns, and the raw-transcript parser keeps its tool_name_by_call_id
    # map across turns so a tool_result in turn N can still be labeled with
    # the tool name declared in turn N-1's assistant message.
    seen_bytes = 0
    parser = RawTranscriptParser(
        warner=MalformedJsonLineWarner(source_description=f"raw transcript for agent {agent.name}"),
    )
    read_failure_warner = _TranscriptReadFailureWarner()
    with _DestroyOnSignal(state=state):
        try:
            final_state, seen_bytes = _wait_for_turn_end(
                agent, events_target, writer, parser, read_failure_warner, seen_bytes, stream_consumer
            )
            if stream_consumer is not None:
                stream_consumer.flush()
            for next_prompt in remaining_prompts:
                if final_state != AgentLifecycleState.WAITING:
                    # Agent already terminated; sending another prompt would just
                    # produce a confusing delivery error that hides the real cause.
                    # The prompt has already been pulled off the input iterator, so
                    # surface a warning rather than silently dropping it.
                    logger.warning(
                        "Discarding pending user prompt because agent {} terminated in state {} "
                        "before reaching WAITING; the prompt had already been consumed from stdin.",
                        agent.name,
                        final_state.value,
                    )
                    break
                _send_user_turn(mngr_ctx, agent, next_prompt)
                turn_count += 1
                final_state, seen_bytes = _wait_for_turn_end(
                    agent, events_target, writer, parser, read_failure_warner, seen_bytes, stream_consumer
                )
                if stream_consumer is not None:
                    stream_consumer.flush()
        except MngrError as exc:
            logger.error("Run failed: {}", exc)
            _finalize_run(writer, start_time, agent_id=str(agent.id), error_text=str(exc), turn_count=turn_count)
            return EXIT_MNGR_ERROR

    if final_state != AgentLifecycleState.WAITING:
        error_text = f"agent ended in state {final_state.value} before reaching WAITING"
        logger.error("{}", error_text)
        _finalize_run(writer, start_time, agent_id=str(agent.id), error_text=error_text, turn_count=turn_count)
        return EXIT_CLAUDE_ERROR

    _finalize_run(writer, start_time, agent_id=str(agent.id), error_text=None, turn_count=turn_count)
    return EXIT_SUCCESS


def _build_agent_name() -> AgentName:
    """Auto-generate a name with the ``robinhood-`` prefix."""
    base = generate_agent_name(AgentNameStyle.COOLNAME)
    return AgentName(f"robinhood-{base}")


def _send_user_turn(mngr_ctx: MngrContext, agent: ClaudeAgent, prompt: str) -> None:
    """Deliver a follow-up prompt to the running agent via ``send_message_to_agents``."""
    # The orchestrator only runs against locally-created Claude agents (see
    # _build_events_target), so the host and provider are fixed; no discovery
    # round-trip is needed to construct the AgentMatch.
    match = AgentMatch(
        agent_id=agent.id,
        agent_name=agent.name,
        host_id=agent.host_id,
        host_name=HostName(LOCAL_HOST_NAME),
        provider_name=LOCAL_PROVIDER_NAME,
    )
    result = send_message_to_agents(
        mngr_ctx=mngr_ctx,
        message_content=prompt,
        agents_to_message=(match,),
        error_behavior=ErrorBehavior.ABORT,
        is_start_desired=False,
    )
    if result.failed_agents:
        names_and_errors = "; ".join(f"{name}: {error}" for name, error in result.failed_agents)
        raise MngrError(f"Failed to deliver follow-up prompt to {agent.name}: {names_and_errors}")


class _TurnEndTicker(MutableModel):
    """Per-iteration state for :func:`_wait_for_turn_end`.

    Polls the raw transcript and the agent's lifecycle state on each tick.
    Returns a non-``None`` result when the turn has demonstrably ended; the
    caller maps that result back to the orchestrator's exit code.

    Exit signals (in priority order):

    1. **Terminal assistant message past baseline** -- the writer has seen at
       least one new ``assistant_message`` with a terminal ``stop_reason``
       (``end_turn`` / ``max_tokens`` / ``stop_sequence``) since
       :attr:`baseline_assistant_count`. This is the authoritative
       end-of-turn signal: the LAST message of the turn has been mirrored
       into events.jsonl and we are done.
    2. **Agent died** -- lifecycle state in :data:`AGENT_DEAD_STATES`
       (STOPPED / DONE / REPLACED / RUNNING_UNKNOWN_AGENT_TYPE). The agent
       will never produce another message; the caller treats this as a
       claude-side failure.
    3. **No-progress safety timeout** -- if the writer has not seen a new
       assistant_message for :data:`TURN_END_NO_PROGRESS_TIMEOUT_SECONDS`,
       bail with ``AgentLifecycleState.WAITING`` and a WARNING. This is a
       safety net for pathological cases (``stream_transcript.sh`` dies,
       claude is wedged without writing to its session file, etc.); in
       normal use it should never fire.

    Note that the lifecycle ``WAITING`` state is intentionally NOT a trigger
    here: it flickers briefly during tool-permission auto-approval (the
    ``PermissionRequest`` hook touches ``permissions_waiting`` for a window
    that elevates ``RUNNING`` to ``WAITING``), and even at the real end of
    turn it leads ``stream_transcript.sh``'s mirror by enough that a
    WAITING-gated exit consistently drops the final message. The transcript
    itself is the only fully reliable end-of-turn marker.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    get_lifecycle_state: Callable[[], AgentLifecycleState] = Field(
        description=(
            "Producer for the agent's current lifecycle state. Injected as a callable (rather "
            "than the agent itself) so the ticker doesn't depend on ``ClaudeAgent`` -- tests can "
            "swap in a stub lifecycle source without needing to construct a real agent."
        )
    )
    events_target: EventsTarget = Field(description="Where to read raw transcript bytes from")
    writer: StreamingOutputWriter = Field(description="Writer whose assistant counters the ticker inspects")
    parser: RawTranscriptParser = Field(description="Parser to convert raw transcript lines to common events")
    read_failure_warner: _TranscriptReadFailureWarner = Field(
        description="Throttler for transcript-read failure warnings"
    )
    stream_consumer: _StreamBufferConsumer | None = Field(
        default=None,
        description="Optional consumer that emits incremental stream_buffer deltas each tick",
    )
    baseline_assistant_count: int = Field(
        description="``writer.assistant_message_count`` snapshot taken before the current turn"
    )
    seen_bytes: int = Field(description="Byte offset already consumed from the raw transcript")
    last_progress_count: int = Field(
        default=0,
        description=(
            "``writer.assistant_message_count`` as of the last tick that observed forward progress. "
            "Used together with :attr:`last_progress_at` to fire the no-progress safety timeout only "
            "when the transcript has genuinely stalled, not just because a turn is taking a while."
        ),
    )
    last_progress_at: float = Field(
        default_factory=time.monotonic,
        description="``time.monotonic()`` snapshot of the last tick that observed forward progress",
    )
    no_progress_timeout_seconds: float = Field(
        default=TURN_END_NO_PROGRESS_TIMEOUT_SECONDS,
        description="Bail after this many seconds without any new assistant_message events",
    )

    def tick(self) -> AgentLifecycleState | None:
        if self.stream_consumer is not None:
            self.stream_consumer.poll()
        self.seen_bytes = _drain_new_events(
            self.events_target,
            self.writer,
            self.parser,
            self.read_failure_warner,
            self.seen_bytes,
        )
        if self.writer.assistant_message_count > self.last_progress_count:
            self.last_progress_count = self.writer.assistant_message_count
            self.last_progress_at = time.monotonic()
        if (
            self.writer.assistant_message_count > self.baseline_assistant_count
            and self.writer.last_assistant_stop_reason in TERMINAL_STOP_REASONS
        ):
            return AgentLifecycleState.WAITING
        state = self.get_lifecycle_state()
        if state in AGENT_DEAD_STATES:
            return state
        if time.monotonic() - self.last_progress_at > self.no_progress_timeout_seconds:
            logger.warning(
                "Turn-end safety timeout: no new assistant_message events for {:.1f}s "
                "(assistant_message_count={}, baseline={}, last_stop_reason={!r}); "
                "finalizing with what we have",
                self.no_progress_timeout_seconds,
                self.writer.assistant_message_count,
                self.baseline_assistant_count,
                self.writer.last_assistant_stop_reason,
            )
            return AgentLifecycleState.WAITING
        return None


def _wait_for_turn_end(
    agent: ClaudeAgent,
    events_target: EventsTarget,
    writer: StreamingOutputWriter,
    parser: RawTranscriptParser,
    read_failure_warner: _TranscriptReadFailureWarner,
    seen_bytes: int,
    stream_consumer: _StreamBufferConsumer | None = None,
) -> tuple[AgentLifecycleState, int]:
    """Poll the raw transcript until the turn's terminal assistant message arrives.

    Returns ``(final_state, new_seen_bytes)``. The success path returns
    :data:`AgentLifecycleState.WAITING` -- the canonical "turn over, ready
    for next prompt" state. Any state in :data:`AGENT_DEAD_STATES`
    (STOPPED / DONE / REPLACED / RUNNING_UNKNOWN_AGENT_TYPE) is the failure
    path: the agent died mid-turn and the caller treats this as a claude-
    side failure. The returned offset must be threaded back into the next
    call so multi-turn invocations do not re-read prior turns' transcript
    bytes.

    The previous version of this function gated finalization on mngr's
    lifecycle ``WAITING`` signal (the ``active`` file being absent). That
    signal is unreliable for two reasons:

    1. It flickers during tool-permission auto-approval, so a WAITING-gated
       exit can fire mid-turn while a tool is running.
    2. Even at the real end of turn it leads ``stream_transcript.sh``'s
       1-second-cadence mirror by enough that the final assistant message
       frequently hasn't been copied into events.jsonl yet, producing an
       empty ``result`` envelope.

    Instead we poll the transcript directly and look for the only signal
    that's guaranteed reliable: an ``assistant_message`` event whose
    ``stop_reason`` is terminal (``end_turn`` / ``max_tokens`` /
    ``stop_sequence``). Lifecycle is consulted only as a fallback to detect
    agent death.
    """
    ticker = _TurnEndTicker(
        get_lifecycle_state=agent.get_lifecycle_state,
        events_target=events_target,
        writer=writer,
        parser=parser,
        read_failure_warner=read_failure_warner,
        baseline_assistant_count=writer.assistant_message_count,
        seen_bytes=seen_bytes,
        last_progress_count=writer.assistant_message_count,
        stream_consumer=stream_consumer,
    )
    # ``poll_for_value`` requires a finite ``timeout``; we want the ticker
    # itself to decide when to stop (via the no-progress safety check), so
    # we pass a deliberately oversized outer timeout that the ticker will
    # never reach in normal operation. If a turn does run longer than this,
    # ``poll_for_value`` returns ``None`` and we treat that the same as the
    # safety timeout firing inside the ticker.
    outer_timeout_seconds = ticker.no_progress_timeout_seconds * 10
    result, _, _ = poll_for_value(
        producer=ticker.tick,
        timeout=outer_timeout_seconds,
        poll_interval=POLL_INTERVAL_SECONDS,
    )
    if result is None:
        logger.warning(
            "Turn-end outer timeout ({:.0f}s) exceeded; finalizing as WAITING",
            outer_timeout_seconds,
        )
        return AgentLifecycleState.WAITING, ticker.seen_bytes
    return result, ticker.seen_bytes


def _drain_new_events(
    events_target: EventsTarget,
    writer: StreamingOutputWriter,
    parser: RawTranscriptParser,
    read_failure_warner: _TranscriptReadFailureWarner,
    seen_bytes: int,
) -> int:
    """Read the raw transcript file, emit any new events past ``seen_bytes``, return new offset.

    Only consumes complete newline-terminated lines; any trailing partial line
    (a write that has not yet been flushed by ``stream_transcript.sh``) is held
    back until the next poll, so we do not silently drop in-flight events.
    The offset is tracked in UTF-8 bytes to match :func:`split_complete_lines`.
    """
    try:
        content = read_event_content(events_target, RAW_TRANSCRIPT_PATH)
    except MngrError as exc:
        # ``read_event_content`` reads the transcript via ``cat`` on the
        # agent's online host. Before ``stream_transcript.sh`` has produced
        # the raw transcript file, ``cat`` exits with
        # "No such file or directory" and the API turns that into an
        # ``MngrError``. That case is benign during the normal startup
        # window and must not flood the log on every poll; everything else
        # is a real read failure worth surfacing once.
        if "No such file or directory" in str(exc):
            logger.trace("raw transcript not yet available at {}", RAW_TRANSCRIPT_PATH)
        else:
            read_failure_warner.warn(exc)
        return seen_bytes
    content_bytes = content.encode("utf-8")
    if len(content_bytes) <= seen_bytes:
        return seen_bytes
    new_slice = content_bytes[seen_bytes:].decode("utf-8", errors="replace")
    new_lines, consumed_bytes = split_complete_lines(new_slice)
    if consumed_bytes == 0:
        # Only a partial line so far; wait for the writer to flush a newline.
        return seen_bytes
    new_events = parser.parse_lines(new_lines)
    if new_events:
        writer.emit_events(new_events)
    return seen_bytes + consumed_bytes


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


def _finalize_run(
    writer: StreamingOutputWriter,
    start_time: float,
    agent_id: str,
    error_text: str | None,
    turn_count: int,
) -> None:
    """Build the result metadata for this run and flush the writer's trailing envelope."""
    meta = _build_result_meta(start_time, agent_id=agent_id, error_text=error_text)
    writer.finalize(meta, turn_count=turn_count)


class _DestroyOnSignal(MutableModel):
    """Context manager: traps SIGINT/SIGTERM, destroys the agent, re-raises.

    The handler closes over the state via the instance, so it does not need
    to be defined as a nested function. Original signal handlers are
    restored on exit so the wrapper plays nicely with parents that install
    their own.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

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
        destroy_agent(self.state.agent, self.state.host)
        signal.signal(signal.SIGINT, self.original_int)
        signal.signal(signal.SIGTERM, self.original_term)
        os.kill(os.getpid(), signum)
