import io
import json
import threading
import time
from pathlib import Path

import pytest

from imbue.mngr.api.events import EventsTarget
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr_claude.stream_buffer import SnapshotDeltaReader
from imbue.mngr_robinhood.agent_runtime import build_pass_env_vars
from imbue.mngr_robinhood.data_types import OutputFormat
from imbue.mngr_robinhood.orchestrator import _StreamBufferConsumer
from imbue.mngr_robinhood.orchestrator import _TranscriptReadFailureWarner
from imbue.mngr_robinhood.orchestrator import _TurnEndTicker
from imbue.mngr_robinhood.orchestrator import _build_agent_name
from imbue.mngr_robinhood.orchestrator import _build_result_meta
from imbue.mngr_robinhood.orchestrator import monotonic_ms_since
from imbue.mngr_robinhood.output_modes import StreamingOutputWriter
from imbue.mngr_robinhood.raw_transcript import RawTranscriptParser


def test_build_agent_name_has_robinhood_prefix() -> None:
    name = _build_agent_name()
    assert str(name).startswith("robinhood-")
    assert len(str(name)) > len("robinhood-")


class _FakeBufferHost:
    """Minimal host stand-in whose read_text_file returns a settable buffer."""

    def __init__(self) -> None:
        self.content = ""

    def read_text_file(self, _path: Path) -> str:
        return self.content


def _drive_consumer_turn(consumer: _StreamBufferConsumer, host: _FakeBufferHost, snapshots: list[str]) -> None:
    """Feed a turn's buffer snapshots through poll(), then empty + flush()."""
    for snapshot in snapshots:
        host.content = snapshot
        consumer.poll()
    # The watcher empties the body at idle (id line only).
    host.content = "id\n"
    consumer.poll()
    consumer.flush()


def test_stream_consumer_emits_full_new_message_sharing_prefix_across_turns() -> None:
    # A new turn's message that shares a leading prefix with the previous turn's
    # message must be emitted whole, not truncated to the diverging suffix. The
    # consumer resets its emitted/last-content state at flush() so each turn diffs
    # from an empty baseline.
    stdout = io.StringIO()
    writer = StreamingOutputWriter(
        output_format=OutputFormat.TEXT, session_id="agent-x", stdout=stdout, stream_plain_text=True
    )
    host = _FakeBufferHost()
    consumer = _StreamBufferConsumer.model_construct(
        host=host, buffer_path=Path("/buffer"), writer=writer, reader=SnapshotDeltaReader()
    )

    _drive_consumer_turn(consumer, host, ["id1\nThe answer is\n", "id1\nThe answer is 42.\nx"])
    # The continuation reflows "The answer is" onto the same line as "42."; the
    # already-printed newline cannot be unprinted, and the redundant whitespace at
    # the reflow boundary is collapsed rather than re-emitted.
    assert stdout.getvalue() == "The answer is\n42.\nx"

    stdout.truncate(0)
    stdout.seek(0)
    _drive_consumer_turn(consumer, host, ["id2\nThe answer is\n", "id2\nThe answer is right.\nx"])
    # The full new message is emitted; the shared "The answer is" prefix is not
    # stripped (which would have produced just "right.\nx" without the prefix).
    assert stdout.getvalue() == "The answer is\nright.\nx"

    # A subsequent turn with no new output must not re-emit the prior content.
    stdout.truncate(0)
    stdout.seek(0)
    _drive_consumer_turn(consumer, host, [])
    assert stdout.getvalue() == ""


def test_build_pass_env_vars_drops_kitty_terminal_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    # KITTY_* terminal-emulator vars (notably KITTY_SHELL_INTEGRATION) wedge a headless tmux
    # agent's login-shell startup, so they must not be forwarded into the sourced env file.
    monkeypatch.setenv("KITTY_SHELL_INTEGRATION", "enabled")
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    monkeypatch.setenv("ROBINHOOD_TEST_FORWARDABLE_VAR", "keep-me")
    options = build_pass_env_vars()
    forwarded_keys = {env_var.key for env_var in options.env_vars}
    assert not any(key.startswith("KITTY_") for key in forwarded_keys)
    # Guard against a vacuous test: a benign var injected alongside is still forwarded.
    assert "ROBINHOOD_TEST_FORWARDABLE_VAR" in forwarded_keys


def test_build_pass_env_vars_drops_caller_tmux_session_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    # When ``mngr robinhood`` runs from inside a tmux session, forwarding the caller's TMUX /
    # TMUX_PANE points the spawned (headless, own-tmux) agent's tmux machinery at the parent's pane,
    # so the agent never signals readiness and create hangs. These must not be forwarded.
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,7")
    monkeypatch.setenv("TMUX_PANE", "%48")
    monkeypatch.setenv("ROBINHOOD_TEST_FORWARDABLE_VAR", "keep-me")
    options = build_pass_env_vars()
    forwarded_keys = {env_var.key for env_var in options.env_vars}
    assert "TMUX" not in forwarded_keys
    assert "TMUX_PANE" not in forwarded_keys
    # Guard against a vacuous test: a benign var injected alongside is still forwarded.
    assert "ROBINHOOD_TEST_FORWARDABLE_VAR" in forwarded_keys


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


def test_monotonic_ms_since_scales_to_milliseconds() -> None:
    """Guard the ``* 1000`` scaling without any wall-clock-timing assumption. The internal
    ``time.monotonic()`` read happens between ``lower_ms`` and ``upper_ms``; because monotonic is
    non-decreasing, the result is mathematically bracketed by them regardless of how long the call
    takes, so this is deterministic, not merely improbable-to-flake. A dropped ``* 1000`` would
    return seconds (~1) instead of milliseconds (~1000), falling below the lower bound."""
    start = time.monotonic() - 1.0
    lower_ms = (time.monotonic() - start) * 1000
    result = monotonic_ms_since(start)
    upper_ms = (time.monotonic() - start) * 1000
    assert int(lower_ms) <= result <= upper_ms


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
        host=local_host,
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
        # The 0.25s append delay vs the 2.0s deadline is a deliberate 8x margin:
        # it is the flakiness guard for this wall-clock-dependent test.
        deadline = time.monotonic() + 2.0
        result: AgentLifecycleState | None = None
        while result is None and time.monotonic() < deadline:
            result = ticker.tick()
    finally:
        timer.join(timeout=5.0)

    assert result == AgentLifecycleState.WAITING
    assert writer.last_assistant_stop_reason == "end_turn"
