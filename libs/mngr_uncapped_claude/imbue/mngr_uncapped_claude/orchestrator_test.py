import io
import json
import threading
import time
from pathlib import Path

from imbue.mngr.api.events import EventsTarget
from imbue.mngr.hosts.host import Host
from imbue.mngr.utils.jsonl_warn import MalformedJsonLineWarner
from imbue.mngr_uncapped_claude.data_types import OutputFormat
from imbue.mngr_uncapped_claude.orchestrator import _TURN_END_QUIESCE_TIMEOUT_SECONDS
from imbue.mngr_uncapped_claude.orchestrator import _TranscriptReadFailureWarner
from imbue.mngr_uncapped_claude.orchestrator import _build_agent_name
from imbue.mngr_uncapped_claude.orchestrator import _build_pass_env_vars
from imbue.mngr_uncapped_claude.orchestrator import _build_result_meta
from imbue.mngr_uncapped_claude.orchestrator import _quiesce_after_waiting
from imbue.mngr_uncapped_claude.orchestrator import monotonic_ms_since
from imbue.mngr_uncapped_claude.output_modes import StreamingOutputWriter
from imbue.mngr_uncapped_claude.raw_transcript import RawTranscriptParser


def test_build_agent_name_has_uncapped_prefix() -> None:
    name = _build_agent_name()
    assert str(name).startswith("uncapped-")
    assert len(str(name)) > len("uncapped-")


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
# _quiesce_after_waiting
#
# These tests stand up a real local-host EventsTarget pointed at a tmp file so
# the production read path (events.read_event_content -> cat on the host) is
# fully exercised. The transcript layout matches what stream_transcript.sh
# produces: ``<state_dir>/logs/claude_transcript/events.jsonl``, with the
# events root one level up at ``<state_dir>/events/``.
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


def _make_quiesce_target(local_host: Host, tmp_path: Path) -> tuple[EventsTarget, Path]:
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


def test_quiesce_exits_immediately_when_terminal_event_already_present(local_host: Host, tmp_path: Path) -> None:
    """If the terminal assistant_message is already in the transcript when WAITING
    is observed -- e.g. a long-running turn where stream_transcript.sh caught up
    before idle_prompt fired -- _quiesce_after_waiting returns on its first drain
    with no added latency."""
    target, transcript_path = _make_quiesce_target(local_host, tmp_path)
    transcript_path.write_text(_ASSISTANT_END_TURN_LINE + "\n")
    writer, parser = _make_writer_and_parser()
    read_failure_warner = _TranscriptReadFailureWarner()

    start = time.monotonic()
    new_seen_bytes = _quiesce_after_waiting(
        target, writer, parser, read_failure_warner, seen_bytes=0, baseline_assistant_count=0
    )
    elapsed = time.monotonic() - start

    # One drain, no sleep -- should be sub-second on any reasonable machine.
    assert elapsed < 0.5
    assert writer.assistant_message_count == 1
    assert writer.last_assistant_stop_reason == "end_turn"
    assert new_seen_bytes == len((_ASSISTANT_END_TURN_LINE + "\n").encode("utf-8"))


def test_quiesce_waits_for_terminal_event_to_arrive(local_host: Host, tmp_path: Path) -> None:
    """The transcript is empty when WAITING is observed; a Timer appends
    the terminal event ~250ms later. _quiesce_after_waiting must wait long enough
    to catch it (well under the timeout) and then exit cleanly."""
    target, transcript_path = _make_quiesce_target(local_host, tmp_path)
    writer, parser = _make_writer_and_parser()
    read_failure_warner = _TranscriptReadFailureWarner()

    def _append() -> None:
        with transcript_path.open("a") as fh:
            fh.write(_ASSISTANT_END_TURN_LINE + "\n")

    # threading.Timer delegates the sleep to the stdlib, so the test exercises
    # the production code's polling without adding a bare ``time.sleep`` call
    # to this package's ratchet count.
    timer = threading.Timer(0.25, _append)
    timer.start()
    try:
        start = time.monotonic()
        _quiesce_after_waiting(
            target,
            writer,
            parser,
            read_failure_warner,
            seen_bytes=0,
            baseline_assistant_count=0,
            timeout_seconds=2.0,
        )
        elapsed = time.monotonic() - start
    finally:
        timer.join(timeout=5.0)

    # Should exit shortly after the 0.25s append, well before the 2s timeout.
    assert 0.2 < elapsed < 1.5
    assert writer.assistant_message_count == 1
    assert writer.last_assistant_stop_reason == "end_turn"


def test_quiesce_ignores_non_terminal_stop_reason(local_host: Host, tmp_path: Path) -> None:
    """A tool_use assistant_message satisfies the count gate but is not a
    terminal stop_reason -- the quiesce must keep waiting (and ultimately time
    out, since this test never appends a terminal one)."""
    target, transcript_path = _make_quiesce_target(local_host, tmp_path)
    transcript_path.write_text(_ASSISTANT_TOOL_USE_LINE + "\n")
    writer, parser = _make_writer_and_parser()
    read_failure_warner = _TranscriptReadFailureWarner()

    start = time.monotonic()
    _quiesce_after_waiting(
        target,
        writer,
        parser,
        read_failure_warner,
        seen_bytes=0,
        baseline_assistant_count=0,
        timeout_seconds=0.3,
    )
    elapsed = time.monotonic() - start

    assert elapsed >= 0.3
    assert writer.assistant_message_count == 1
    assert writer.last_assistant_stop_reason == "tool_use"


def test_quiesce_times_out_when_no_events_arrive(local_host: Host, tmp_path: Path) -> None:
    """If the transcript stays empty for the entire quiesce window, return with
    an empty assistant_message_count. The orchestrator finalizes with whatever
    text the writer collected up to that point (possibly nothing) so the user
    still gets a result envelope rather than a hang."""
    target, _ = _make_quiesce_target(local_host, tmp_path)
    writer, parser = _make_writer_and_parser()
    read_failure_warner = _TranscriptReadFailureWarner()

    start = time.monotonic()
    _quiesce_after_waiting(
        target,
        writer,
        parser,
        read_failure_warner,
        seen_bytes=0,
        baseline_assistant_count=0,
        timeout_seconds=0.3,
    )
    elapsed = time.monotonic() - start

    assert elapsed >= 0.3
    assert writer.assistant_message_count == 0
    assert writer.last_assistant_stop_reason is None


def test_quiesce_respects_baseline_so_old_terminal_event_does_not_satisfy(local_host: Host, tmp_path: Path) -> None:
    """Multi-turn replay regression: a terminal assistant_message from a PRIOR
    turn must not satisfy the current turn's quiesce. The orchestrator snapshots
    ``writer.assistant_message_count`` at turn start; the gate requires the
    count to grow past that snapshot before returning."""
    target, transcript_path = _make_quiesce_target(local_host, tmp_path)
    transcript_path.write_text(_ASSISTANT_END_TURN_LINE + "\n")
    writer, parser = _make_writer_and_parser()
    read_failure_warner = _TranscriptReadFailureWarner()

    # Simulate the prior turn: the writer has already seen one end_turn event.
    writer.assistant_message_count = 1
    writer.last_assistant_stop_reason = "end_turn"

    start = time.monotonic()
    _quiesce_after_waiting(
        target,
        writer,
        parser,
        read_failure_warner,
        seen_bytes=len((_ASSISTANT_END_TURN_LINE + "\n").encode("utf-8")),
        baseline_assistant_count=1,
        timeout_seconds=0.3,
    )
    elapsed = time.monotonic() - start

    # Nothing new in the transcript past seen_bytes -- should time out.
    assert elapsed >= 0.3


def test_turn_end_quiesce_timeout_constant_is_at_least_one_stream_poll_interval() -> None:
    """stream_transcript.sh polls per-session JSONL every 1s; the quiesce timeout
    must give us at least that long so a worst-case-aligned race is bridged."""
    assert _TURN_END_QUIESCE_TIMEOUT_SECONDS >= 1.0
