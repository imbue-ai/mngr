import io
import json
import threading
import time
from pathlib import Path

from imbue.mngr.api.events import EventsTarget
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr_robinhood.data_types import OutputFormat
from imbue.mngr_robinhood.orchestrator import _TranscriptReadFailureWarner
from imbue.mngr_robinhood.orchestrator import _TurnEndTicker
from imbue.mngr_robinhood.orchestrator import _build_agent_name
from imbue.mngr_robinhood.orchestrator import _build_pass_env_vars
from imbue.mngr_robinhood.orchestrator import _build_result_meta
from imbue.mngr_robinhood.orchestrator import compute_stream_delta
from imbue.mngr_robinhood.orchestrator import monotonic_ms_since
from imbue.mngr_robinhood.output_modes import StreamingOutputWriter
from imbue.mngr_robinhood.raw_transcript import RawTranscriptParser


def test_build_agent_name_has_robinhood_prefix() -> None:
    name = _build_agent_name()
    assert str(name).startswith("robinhood-")
    assert len(str(name)) > len("robinhood-")


def test_compute_stream_delta_first_emission() -> None:
    delta, emitted = compute_stream_delta("uuid-1\nHello world", "")
    assert delta == "Hello world"
    assert emitted == "Hello world"


def test_compute_stream_delta_prefix_extension() -> None:
    delta, emitted = compute_stream_delta("uuid-1\nHello world, more", "Hello world")
    assert delta == ", more"
    assert emitted == "Hello world, more"


def test_compute_stream_delta_no_change() -> None:
    delta, emitted = compute_stream_delta("uuid-1\nHello", "Hello")
    assert delta == ""
    assert emitted == "Hello"


def test_compute_stream_delta_new_message_resets() -> None:
    # A non-prefix body means a new message scrolled in: emit the whole new body.
    delta, emitted = compute_stream_delta("uuid-2\nA brand new reply", "An old reply")
    assert delta == "A brand new reply"
    assert emitted == "A brand new reply"


def test_compute_stream_delta_empty_body_after_idle() -> None:
    # When the watcher empties the body at turn end, only the id line remains.
    delta, emitted = compute_stream_delta("uuid-1", "previous text")
    assert delta == ""
    assert emitted == ""


def test_compute_stream_delta_multiline_body() -> None:
    delta, emitted = compute_stream_delta("uuid-1\nline one\nline two", "line one")
    assert delta == "\nline two"
    assert emitted == "line one\nline two"


def test_build_pass_env_vars_is_populated() -> None:
    options = _build_pass_env_vars()
    assert len(options.env_vars) > 0


def test_build_result_meta_records_error_text() -> None:
    meta = _build_result_meta(start_time=0.0, agent_id="agent-x", error_text="boom")
    assert meta.is_error
    assert meta.error_text == "boom"
    assert meta.session_id == "agent-x"


def test_build_result_meta_no_error() -> None:
    meta = _build_result_meta(start_time=0.0, agent_id="agent-x", error_text=None)
    assert not meta.is_error
    assert meta.error_text is None


def test_monotonic_ms_since_returns_non_negative_int() -> None:
    start = time.monotonic()
    elapsed = monotonic_ms_since(start)
    assert isinstance(elapsed, int)
    assert elapsed >= 0


def test_transcript_read_failure_warner_warns_once() -> None:
    warner = _TranscriptReadFailureWarner()
    assert not warner.has_warned
    warner.warn(RuntimeError("first failure"))
    assert warner.has_warned
    # Subsequent calls must not flip the flag back off or otherwise raise.
    warner.warn(RuntimeError("second failure"))
    assert warner.has_warned


# =============================================================================
# _TurnEndTicker
#
# These tests stand up a real local-host EventsTarget pointed at a tmp file so
# the production read path (events.read_event_content -> cat on the host) is
# fully exercised. The transcript layout matches what stream_transcript.sh
# produces: ``<state_dir>/logs/claude_transcript/events.jsonl``, with the
# events root one level up at ``<state_dir>/events/``. The agent's lifecycle
# state is injected as a callable so the tests don't need to construct a
# real ClaudeAgent.
# =============================================================================

_ASSISTANT_END_TURN_LINE = json.dumps(
    {
        "type": "assistant",
        "uuid": "u-end",
        "timestamp": "2026-05-16T12:00:00.000Z",
        "message": {
            "content": [{"type": "text", "text": "hello world"}],
            "model": "claude-test",
            "stop_reason": "end_turn",
        },
    }
)

_ASSISTANT_TOOL_USE_LINE = json.dumps(
    {
        "type": "assistant",
        "uuid": "u-tool",
        "timestamp": "2026-05-16T12:00:00.000Z",
        "message": {
            "content": [{"type": "text", "text": "calling a tool"}],
            "model": "claude-test",
            "stop_reason": "tool_use",
        },
    }
)


def _make_ticker_target(local_host: Host, tmp_path: Path) -> tuple[EventsTarget, Path]:
    """Build a local-host EventsTarget plus the per-session transcript path that
    stream_transcript.sh would write to.

    The raw-transcript path the orchestrator reads is
    ``<events_path>/../logs/claude_transcript/events.jsonl`` (resolved by the
    kernel when ``cat`` runs). Mirror that layout so the path passed to ``cat``
    actually exists.
    """
    state_dir = tmp_path / "agent-x"
    events_path = state_dir / "events"
    events_path.mkdir(parents=True)
    transcript_path = state_dir / "logs" / "claude_transcript" / "events.jsonl"
    transcript_path.parent.mkdir(parents=True)
    transcript_path.write_text("")
    target = EventsTarget(
        online_host=local_host,
        events_path=events_path,
        display_name="agent 'agent-x'",
    )
    return target, transcript_path


def _make_writer_and_parser() -> tuple[StreamingOutputWriter, RawTranscriptParser]:
    stdout = io.StringIO()
    writer = StreamingOutputWriter(output_format=OutputFormat.TEXT, session_id="agent-x", stdout=stdout)
    parser = RawTranscriptParser(warner=MalformedJsonLineWarner(source_description="test transcript"))
    return writer, parser


def _make_ticker(
    target: EventsTarget,
    writer: StreamingOutputWriter,
    parser: RawTranscriptParser,
    *,
    baseline_assistant_count: int = 0,
    seen_bytes: int = 0,
    lifecycle_state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    no_progress_timeout_seconds: float = 0.5,
) -> _TurnEndTicker:
    """Construct a ticker for a test, with a constant lifecycle source.

    Tests that want to flip the lifecycle mid-run construct the ticker
    directly and pass their own stateful callable.
    """
    return _TurnEndTicker(
        get_lifecycle_state=lambda: lifecycle_state,
        events_target=target,
        writer=writer,
        parser=parser,
        read_failure_warner=_TranscriptReadFailureWarner(),
        baseline_assistant_count=baseline_assistant_count,
        seen_bytes=seen_bytes,
        last_progress_count=baseline_assistant_count,
        no_progress_timeout_seconds=no_progress_timeout_seconds,
    )


def test_ticker_exits_on_terminal_stop_reason(local_host: Host, tmp_path: Path) -> None:
    """The authoritative end-of-turn signal: a new assistant_message past the
    baseline whose stop_reason is terminal. The ticker returns WAITING and
    seen_bytes advances past the consumed line."""
    target, transcript_path = _make_ticker_target(local_host, tmp_path)
    transcript_path.write_text(_ASSISTANT_END_TURN_LINE + "\n")
    writer, parser = _make_writer_and_parser()
    ticker = _make_ticker(target, writer, parser)

    result = ticker.tick()

    assert result == AgentLifecycleState.WAITING
    assert writer.assistant_message_count == 1
    assert writer.last_assistant_stop_reason == "end_turn"
    assert ticker.seen_bytes == len((_ASSISTANT_END_TURN_LINE + "\n").encode("utf-8"))


def test_ticker_does_not_exit_on_tool_use_stop_reason(local_host: Host, tmp_path: Path) -> None:
    """A tool_use assistant_message is mid-turn -- more events are still
    coming -- so the ticker must NOT return. This is the key behavioral
    difference from the previous WAITING-gated design: the lifecycle state
    would have triggered an exit here (the ``active`` file flicker-clears
    during permission auto-approval), but the transcript signal correctly
    reports "not done yet"."""
    target, transcript_path = _make_ticker_target(local_host, tmp_path)
    transcript_path.write_text(_ASSISTANT_TOOL_USE_LINE + "\n")
    writer, parser = _make_writer_and_parser()
    ticker = _make_ticker(target, writer, parser, lifecycle_state=AgentLifecycleState.WAITING)

    result = ticker.tick()

    assert result is None
    assert writer.assistant_message_count == 1
    assert writer.last_assistant_stop_reason == "tool_use"


def test_ticker_exits_on_dead_agent_state(local_host: Host, tmp_path: Path) -> None:
    """If the agent dies mid-turn (STOPPED / DONE / REPLACED / RUNNING_UNKNOWN),
    the ticker returns that state so the caller can surface a claude-side
    failure. The terminal-stop-reason check has priority, but with an empty
    transcript the lifecycle fallback is what fires."""
    target, _ = _make_ticker_target(local_host, tmp_path)
    writer, parser = _make_writer_and_parser()
    ticker = _make_ticker(target, writer, parser, lifecycle_state=AgentLifecycleState.STOPPED)

    result = ticker.tick()

    assert result == AgentLifecycleState.STOPPED


def test_ticker_respects_baseline_for_multi_turn(local_host: Host, tmp_path: Path) -> None:
    """A terminal assistant_message from a PRIOR turn must not satisfy the
    current turn's exit gate. The ticker baselines off
    ``writer.assistant_message_count`` at turn start; only growth past that
    baseline counts as "current-turn progress"."""
    target, transcript_path = _make_ticker_target(local_host, tmp_path)
    transcript_path.write_text(_ASSISTANT_END_TURN_LINE + "\n")
    writer, parser = _make_writer_and_parser()
    # Simulate the prior turn: writer already saw the end_turn event and we
    # already consumed the bytes for it.
    writer.assistant_message_count = 1
    writer.last_assistant_stop_reason = "end_turn"
    ticker = _make_ticker(
        target,
        writer,
        parser,
        baseline_assistant_count=1,
        seen_bytes=len((_ASSISTANT_END_TURN_LINE + "\n").encode("utf-8")),
    )

    result = ticker.tick()

    # Nothing new past seen_bytes -- ticker keeps waiting.
    assert result is None


def test_ticker_fires_no_progress_safety_timeout(local_host: Host, tmp_path: Path) -> None:
    """If the transcript never grows, the safety timeout fires and the ticker
    returns WAITING so the orchestrator can finalize with whatever was
    collected. In practice this safeguards against ``stream_transcript.sh``
    dying or the agent being wedged without writing to its session file."""
    target, _ = _make_ticker_target(local_host, tmp_path)
    writer, parser = _make_writer_and_parser()
    ticker = _make_ticker(target, writer, parser, no_progress_timeout_seconds=0.0)
    # Drive the clock so the no-progress window appears to have already elapsed.
    ticker.last_progress_at = time.monotonic() - 1.0

    result = ticker.tick()

    assert result == AgentLifecycleState.WAITING


def test_ticker_resets_no_progress_clock_when_events_arrive(local_host: Host, tmp_path: Path) -> None:
    """The no-progress safety timeout is measured from the most recent new
    assistant_message, not from turn start. A turn that keeps producing tool
    cycles (non-terminal stop_reasons) for longer than the timeout must NOT
    be considered stalled, as long as new events keep showing up."""
    target, transcript_path = _make_ticker_target(local_host, tmp_path)
    writer, parser = _make_writer_and_parser()
    ticker = _make_ticker(target, writer, parser, no_progress_timeout_seconds=0.5)
    # Backdate the progress clock so the timeout WOULD have fired if not reset.
    ticker.last_progress_at = time.monotonic() - 1.0
    # Append a tool-use event so this tick observes forward progress.
    transcript_path.write_text(_ASSISTANT_TOOL_USE_LINE + "\n")

    result = ticker.tick()

    # Ticker observed forward progress, so it must NOT have fired the safety
    # timeout. Returns None because tool_use is non-terminal.
    assert result is None
    assert ticker.last_progress_count == 1


def test_ticker_terminal_stop_takes_priority_over_dead_state(local_host: Host, tmp_path: Path) -> None:
    """If a terminal assistant_message has arrived AND the agent has also gone
    STOPPED in the same tick (race during shutdown), report the success path.
    The transcript signal is authoritative; the lifecycle is only a fallback
    for cases where the transcript never produces a terminal."""
    target, transcript_path = _make_ticker_target(local_host, tmp_path)
    transcript_path.write_text(_ASSISTANT_END_TURN_LINE + "\n")
    writer, parser = _make_writer_and_parser()
    ticker = _make_ticker(target, writer, parser, lifecycle_state=AgentLifecycleState.STOPPED)

    result = ticker.tick()

    assert result == AgentLifecycleState.WAITING


def test_ticker_picks_up_terminal_event_appended_during_polling(local_host: Host, tmp_path: Path) -> None:
    """End-to-end-ish: the ticker is invoked repeatedly while a Timer appends
    the terminal event ~250ms in. The ticker returns ``None`` on the early
    ticks and then ``WAITING`` once the line appears. This exercises the
    transcript-driven polling that replaces the old WAITING-gated quiesce."""
    target, transcript_path = _make_ticker_target(local_host, tmp_path)
    writer, parser = _make_writer_and_parser()
    ticker = _make_ticker(target, writer, parser, no_progress_timeout_seconds=5.0)

    def _append() -> None:
        with transcript_path.open("a") as fh:
            fh.write(_ASSISTANT_END_TURN_LINE + "\n")

    # threading.Timer delegates the sleep to stdlib so the test exercises the
    # ticker's polling without adding a bare ``time.sleep`` call to this
    # package's ratchet count.
    timer = threading.Timer(0.25, _append)
    timer.start()
    try:
        deadline = time.monotonic() + 2.0
        result: AgentLifecycleState | None = None
        while result is None and time.monotonic() < deadline:
            result = ticker.tick()
    finally:
        timer.join(timeout=5.0)

    assert result == AgentLifecycleState.WAITING
    assert writer.last_assistant_stop_reason == "end_turn"
