import json
import os
import re
import sys
import threading
import time
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.profiling import ClockSync
from imbue.imbue_common.profiling import ProfilingDisabledError
from imbue.imbue_common.profiling import ProfilingFlowError
from imbue.imbue_common.profiling import ProfilingModeError
from imbue.imbue_common.profiling import ProfilingTimestampError
from imbue.imbue_common.profiling import TraceContext
from imbue.imbue_common.profiling import _CollectorProfiler
from imbue.imbue_common.profiling import _WriterProfiler
from imbue.imbue_common.profiling import _default_process_name
from imbue.imbue_common.profiling import capture_clock_sync
from imbue.imbue_common.profiling import disable
from imbue.imbue_common.profiling import drain_events
from imbue.imbue_common.profiling import enable_collector
from imbue.imbue_common.profiling import enable_writer
from imbue.imbue_common.profiling import get_profiler
from imbue.imbue_common.profiling import mint_span_id
from imbue.imbue_common.profiling import mint_trace_id
from imbue.imbue_common.profiling import now_us
from imbue.imbue_common.profiling import parse_traceparent
from imbue.imbue_common.profiling import profiled
from imbue.imbue_common.profiling import rebase_epoch_us
from imbue.imbue_common.profiling import render_traceparent

_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")


def test_now_us_is_the_perf_counter_in_integer_microseconds() -> None:
    before_ns = time.perf_counter_ns()
    reading = now_us()
    after_ns = time.perf_counter_ns()

    assert isinstance(reading, int)
    assert before_ns // 1000 <= reading <= after_ns // 1000


def test_mint_trace_id_is_32_lowercase_hex_and_unique() -> None:
    minted = {mint_trace_id() for _ in range(100)}

    assert len(minted) == 100
    assert all(_TRACE_ID_PATTERN.match(trace_id) for trace_id in minted)
    assert "0" * 32 not in minted


def test_mint_span_id_is_16_lowercase_hex_and_unique() -> None:
    minted = {mint_span_id() for _ in range(100)}

    assert len(minted) == 100
    assert all(_SPAN_ID_PATTERN.match(span_id) for span_id in minted)
    assert "0" * 16 not in minted


def test_parse_traceparent_round_trips_through_render_traceparent() -> None:
    context = TraceContext(trace_id="4bf92f3577b34da6a3ce929d0e0e4736", span_id="00f067aa0ba902b7")

    rendered = render_traceparent(context)

    assert rendered == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    assert parse_traceparent(rendered) == context


def test_parse_traceparent_accepts_any_trace_flags() -> None:
    parsed = parse_traceparent("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00")

    assert parsed == TraceContext(trace_id="4bf92f3577b34da6a3ce929d0e0e4736", span_id="00f067aa0ba902b7")


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "not a header",
        "01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        "00-4BF92F3577B34DA6A3CE929D0E0E4736-00F067AA0BA902B7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e47-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-1",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra",
        "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
        " 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    ],
)
def test_parse_traceparent_returns_none_for_absent_or_malformed_values(value: str | None) -> None:
    assert parse_traceparent(value) is None


def test_capture_clock_sync_reads_both_clocks_together() -> None:
    epoch_before_ns = time.time_ns()
    perf_before_ns = time.perf_counter_ns()
    sync = capture_clock_sync()
    epoch_after_ns = time.time_ns()
    perf_after_ns = time.perf_counter_ns()

    assert epoch_before_ns <= sync.epoch_ns <= epoch_after_ns
    assert perf_before_ns <= sync.perf_counter_ns <= perf_after_ns


def test_rebase_epoch_us_moves_a_wall_clock_timestamp_onto_the_trace_clock() -> None:
    sync = ClockSync(epoch_ns=1_700_000_000_000_000_000, perf_counter_ns=5_000_000_000)

    # Two and a half milliseconds after the sync point on the wall clock lands the same
    # distance after it on the trace clock.
    rebased = rebase_epoch_us(1_700_000_000_002_500.0, sync)

    assert rebased == 5_002_500
    assert isinstance(rebased, int)


def test_rebase_epoch_us_rounds_to_the_nearest_microsecond() -> None:
    sync = ClockSync(epoch_ns=1_000_000_000, perf_counter_ns=0)

    assert rebase_epoch_us(1_000_000.4, sync) == 0
    assert rebase_epoch_us(1_000_000.6, sync) == 1
    assert rebase_epoch_us(999_999.4, sync) == -1


# Tests for the profiler modes


class _SampleProfilingError(Exception):
    """Sample exception used to exercise the failure path of a span."""


class _ManualClock(MutableModel):
    """A trace clock the test advances by hand, so flush-by-age needs no sleeping."""

    reading_us: int = Field(description="What the clock reads until the test moves it")

    def read(self) -> int:
        return self.reading_us


def _read_trace_file(path: Path) -> list[dict[str, Any]]:
    """Parse a trace file, terminating the array when the writer has not closed it yet."""
    text = path.read_text()
    return json.loads(text if text.rstrip().endswith("]") else text + "]")


def _events_of_phase(events: Sequence[Mapping[str, Any]], phase: str) -> list[Mapping[str, Any]]:
    return [event for event in events if event["ph"] == phase]


def _metadata_names(events: Sequence[Mapping[str, Any]], metadata_name: str) -> list[str]:
    return [event["args"]["name"] for event in _events_of_phase(events, "M") if event["name"] == metadata_name]


def _make_collector(max_event_count: int) -> _CollectorProfiler:
    return _CollectorProfiler(clock_sync_at_enable=capture_clock_sync(), max_event_count=max_event_count)


def _make_writer(path: Path, max_event_count: int, clock: _ManualClock) -> _WriterProfiler:
    return _WriterProfiler(
        path=path,
        clock_sync_at_enable=capture_clock_sync(),
        max_event_count=max_event_count,
        clock_us=clock.read,
    )


def test_profiler_is_a_reusable_noop_until_a_mode_is_enabled() -> None:
    profiler = get_profiler()

    assert profiler.is_enabled is False
    assert profiler.span("first") is profiler.span("second", "cat", flow_in="a", flow_out="a", traceparent=None, x=1)
    with profiler.span("nothing", some_arg=1) as handle:
        handle.add_args(more=2)
        handle.set_flow_out("flow-a")
        handle.set_flow_in("flow-b")
        assert profiler.current_traceparent() is None
    profiler.instant("nothing", count=1)
    profiler.complete("nothing", 10, 5, count=1)
    profiler.set_process_name("ignored")
    profiler.flush()
    with pytest.raises(ProfilingDisabledError):
        _ = profiler.clock_sync
    with pytest.raises(ProfilingDisabledError):
        _ = handle.context
    with pytest.raises(ProfilingModeError):
        profiler.write_foreign_events([{"ph": "i", "name": "foreign"}])
    with pytest.raises(ProfilingModeError):
        drain_events()
    disable()
    assert get_profiler() is profiler


def test_enable_writer_records_spans_and_instants_as_a_chrome_trace_array(tmp_path: Path) -> None:
    trace_path = tmp_path / "nested" / "trace.json"

    enable_writer(trace_path, process_name="writer-test")
    assert get_profiler().is_enabled is True
    with get_profiler().span("outer", "cat-a", answer=42) as handle:
        handle.add_args(is_done=True)
        get_profiler().instant("marker", "cat-b", count=3)
    disable()

    assert get_profiler().is_enabled is False
    text = trace_path.read_text()
    assert text.startswith("[")
    assert text.rstrip().endswith("]")
    events = json.loads(text)
    [span] = _events_of_phase(events, "X")
    assert list(span) == ["ph", "name", "cat", "ts", "dur", "pid", "tid", "args"]
    assert span["name"] == "outer"
    assert span["cat"] == "cat-a"
    assert span["pid"] == os.getpid()
    assert span["tid"] == threading.get_native_id()
    assert isinstance(span["ts"], int)
    assert isinstance(span["dur"], int)
    assert span["args"] == {"answer": 42, "is_done": True}
    [instant] = _events_of_phase(events, "i")
    assert list(instant) == ["ph", "s", "name", "cat", "ts", "pid", "tid", "args"]
    assert instant["s"] == "t"
    assert instant["name"] == "marker"
    assert instant["cat"] == "cat-b"
    assert instant["args"] == {"count": 3}
    assert span["ts"] <= instant["ts"] <= span["ts"] + span["dur"]
    assert _metadata_names(events, "process_name") == ["writer-test"]
    assert _metadata_names(events, "thread_name") == [threading.current_thread().name]
    [thread_metadata] = [event for event in _events_of_phase(events, "M") if event["name"] == "thread_name"]
    assert list(thread_metadata) == ["ph", "name", "pid", "tid", "args"]
    assert thread_metadata["tid"] == threading.get_native_id()


def test_writer_writes_compact_json_events_one_per_line(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"

    enable_writer(trace_path, process_name="compact")
    get_profiler().instant("one")
    get_profiler().instant("two")
    disable()

    lines = trace_path.read_text().splitlines()
    assert lines[0].startswith("[")
    assert lines[-1] == "]"
    assert '"ph":"i","s":"t","name":"one"' in trace_path.read_text()
    event_lines = [line for line in lines if '"name":"one"' in line or '"name":"two"' in line]
    assert len(event_lines) == 2
    assert all(line.endswith(",") for line in lines[:-2])


def test_enable_writer_names_the_process_after_argv_by_default(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"

    enable_writer(trace_path)
    get_profiler().set_process_name("renamed")
    disable()

    events = json.loads(trace_path.read_text())
    assert _metadata_names(events, "process_name") == [_default_process_name(sys.argv), "renamed"]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["/usr/local/bin/minds", "run", "--profile", "x.json"], "minds run"),
        (["/opt/venv/bin/mngr"], "mngr"),
        (["python", "-m"], "python -m"),
        ([], ""),
    ],
)
def test_default_process_name_is_the_executable_base_name_and_first_argument(argv: list[str], expected: str) -> None:
    assert _default_process_name(argv) == expected


def test_enable_writer_overwrites_an_existing_file(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    trace_path.write_text("stale contents that are much longer than an empty trace array")

    enable_writer(trace_path, process_name="fresh")
    disable()

    events = json.loads(trace_path.read_text())
    assert _metadata_names(events, "process_name") == ["fresh"]


def test_enable_collector_buffers_events_and_drain_events_returns_metadata_first() -> None:
    enable_collector(process_name="child")
    with get_profiler().span("work", step=1):
        pass
    get_profiler().set_process_name("child renamed")
    get_profiler().flush()

    events = drain_events()

    assert [event["ph"] for event in events] == ["M", "M", "M", "X"]
    assert _metadata_names(events, "process_name") == ["child", "child renamed"]
    assert _metadata_names(events, "thread_name") == [threading.current_thread().name]
    assert events[3]["name"] == "work"
    assert events[3]["args"] == {"step": 1}
    assert events[3]["pid"] == os.getpid()
    assert drain_events() == []
    disable()
    assert get_profiler().is_enabled is False


def test_enabling_a_mode_while_one_is_enabled_raises(tmp_path: Path) -> None:
    enable_collector()
    with pytest.raises(ProfilingModeError):
        enable_writer(tmp_path / "trace.json")
    with pytest.raises(ProfilingModeError):
        enable_collector()
    with pytest.raises(ProfilingModeError):
        get_profiler().write_foreign_events([{"ph": "i", "name": "foreign"}])
    disable()

    enable_writer(tmp_path / "trace.json")
    with pytest.raises(ProfilingModeError):
        enable_collector()
    with pytest.raises(ProfilingModeError):
        drain_events()
    disable()


def test_clock_sync_is_captured_when_the_mode_is_enabled() -> None:
    perf_before_ns = time.perf_counter_ns()
    enable_collector()
    perf_after_ns = time.perf_counter_ns()

    sync = get_profiler().clock_sync

    assert perf_before_ns <= sync.perf_counter_ns <= perf_after_ns
    assert get_profiler().clock_sync is sync
    disable()


def test_spans_nest_per_thread_and_carry_each_threads_native_id_and_name() -> None:
    enable_collector()
    context_by_label: dict[str, tuple[TraceContext, TraceContext, int, str | None]] = {}

    def record_nested_spans(label: str) -> None:
        with get_profiler().span(f"{label}.outer") as outer:
            with get_profiler().span(f"{label}.inner") as inner:
                context_by_label[label] = (
                    outer.context,
                    inner.context,
                    threading.get_native_id(),
                    get_profiler().current_traceparent(),
                )

    threads = [
        threading.Thread(target=record_nested_spans, args=(label,), name=f"worker-{label}") for label in ("a", "b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    events = drain_events()
    disable()

    for label in ("a", "b"):
        outer_context, inner_context, native_id, innermost_traceparent = context_by_label[label]
        assert inner_context.trace_id == outer_context.trace_id
        assert inner_context.span_id != outer_context.span_id
        assert innermost_traceparent == render_traceparent(inner_context)
        span_tids = {event["tid"] for event in _events_of_phase(events, "X") if event["name"].startswith(label)}
        assert span_tids == {native_id}
        [thread_metadata] = [
            event
            for event in _events_of_phase(events, "M")
            if event["name"] == "thread_name" and event["args"]["name"] == f"worker-{label}"
        ]
        assert thread_metadata["tid"] == native_id
    assert context_by_label["a"][0].trace_id != context_by_label["b"][0].trace_id


def test_thread_name_metadata_is_emitted_once_per_thread() -> None:
    enable_collector()
    get_profiler().instant("first")
    get_profiler().instant("second")
    with get_profiler().span("third"):
        pass

    events = drain_events()
    disable()

    assert _metadata_names(events, "thread_name") == [threading.current_thread().name]


def test_flow_links_mark_the_source_and_target_spans() -> None:
    enable_collector()
    with get_profiler().span("source", flow_out="flow-1"):
        pass
    with get_profiler().span("target", flow_in="flow-1"):
        pass
    with get_profiler().span("relay", flow_in="flow-2", flow_out="flow-2"):
        pass
    with get_profiler().span("plain"):
        pass
    events = drain_events()
    disable()

    span_by_name = {event["name"]: event for event in _events_of_phase(events, "X")}
    assert span_by_name["source"]["bind_id"] == "flow-1"
    assert span_by_name["source"]["flow_out"] is True
    assert "flow_in" not in span_by_name["source"]
    assert span_by_name["target"]["bind_id"] == "flow-1"
    assert span_by_name["target"]["flow_in"] is True
    assert "flow_out" not in span_by_name["target"]
    assert span_by_name["relay"]["bind_id"] == "flow-2"
    assert span_by_name["relay"]["flow_in"] is True
    assert span_by_name["relay"]["flow_out"] is True
    assert not {"bind_id", "flow_in", "flow_out"} & span_by_name["plain"].keys()


def test_a_span_cannot_join_two_different_flows() -> None:
    enable_collector()
    try:
        with pytest.raises(ProfilingFlowError):
            with get_profiler().span("torn", flow_in="flow-1", flow_out="flow-2"):
                pass
    finally:
        disable()


def test_flow_links_can_be_set_on_the_handle_before_the_span_exits() -> None:
    enable_collector()
    with get_profiler().span("source") as source:
        source.set_flow_out(source.context.span_id)
    with get_profiler().span("target") as target:
        target.set_flow_in("flow-1")
    with get_profiler().span("relay", flow_in="flow-2") as relay:
        relay.set_flow_out("flow-2")
    with get_profiler().span("resource") as resource:
        resource.set_flow_out("flow-3")
        resource.set_flow_out("flow-4")
    events = drain_events()
    disable()

    span_by_name = {event["name"]: event for event in _events_of_phase(events, "X")}
    assert span_by_name["source"]["bind_id"] == source.context.span_id
    assert span_by_name["source"]["flow_out"] is True
    assert "flow_in" not in span_by_name["source"]
    assert span_by_name["target"]["bind_id"] == "flow-1"
    assert span_by_name["target"]["flow_in"] is True
    assert span_by_name["relay"]["bind_id"] == "flow-2"
    assert span_by_name["relay"]["flow_in"] is True
    assert span_by_name["relay"]["flow_out"] is True
    assert span_by_name["resource"]["bind_id"] == "flow-4"


def test_setting_a_flow_that_conflicts_with_the_spans_other_flow_raises() -> None:
    enable_collector()
    try:
        with get_profiler().span("torn", flow_in="flow-1") as torn:
            with pytest.raises(ProfilingFlowError):
                torn.set_flow_out("flow-2")
            torn.set_flow_out("flow-1")
        with get_profiler().span("torn too") as torn_too:
            torn_too.set_flow_out("flow-3")
            with pytest.raises(ProfilingFlowError):
                torn_too.set_flow_in("flow-4")
        events = drain_events()
    finally:
        disable()

    span_by_name = {event["name"]: event for event in _events_of_phase(events, "X")}
    assert span_by_name["torn"]["bind_id"] == "flow-1"
    assert span_by_name["torn"]["flow_in"] is True
    assert span_by_name["torn"]["flow_out"] is True
    assert span_by_name["torn too"]["bind_id"] == "flow-3"
    assert "flow_in" not in span_by_name["torn too"]


def test_complete_records_an_already_finished_span_with_the_given_timestamps() -> None:
    enable_collector()
    with get_profiler().span("enclosing"):
        get_profiler().complete("earlier", 1_000, 1_250, "startup", step="import")
    get_profiler().complete("zero length", 2_000, 2_000)
    events = drain_events()
    disable()

    span_by_name = {event["name"]: event for event in _events_of_phase(events, "X")}
    earlier = span_by_name["earlier"]
    assert list(earlier) == ["ph", "name", "cat", "ts", "dur", "pid", "tid", "args"]
    assert earlier["cat"] == "startup"
    assert earlier["ts"] == 1_000
    assert earlier["dur"] == 250
    assert earlier["pid"] == os.getpid()
    assert earlier["tid"] == threading.get_native_id()
    assert earlier["args"] == {"step": "import"}
    assert span_by_name["zero length"]["dur"] == 0
    assert span_by_name["enclosing"]["ts"] > earlier["ts"]
    assert _metadata_names(events, "thread_name") == [threading.current_thread().name]


def test_complete_rejects_an_end_before_its_start() -> None:
    enable_collector()
    try:
        with pytest.raises(ProfilingTimestampError):
            get_profiler().complete("backwards", 2_000, 1_999)
        assert _events_of_phase(drain_events(), "X") == []
    finally:
        disable()


def test_complete_counts_against_the_cap_like_any_own_event() -> None:
    collector = _make_collector(max_event_count=2)

    collector.complete("a", 0, 1)
    collector.complete("b", 1, 2)
    collector.complete("c", 2, 3)
    events = collector.drain()

    assert [event["name"] for event in _events_of_phase(events, "X")] == ["a", "b"]
    assert [event["name"] for event in _events_of_phase(events, "i")] == ["overflow"]


def test_root_span_mints_a_trace_id_and_nested_spans_inherit_it() -> None:
    enable_collector()

    assert get_profiler().current_traceparent() is None
    with get_profiler().span("root") as root:
        assert get_profiler().current_traceparent() == render_traceparent(root.context)
        with get_profiler().span("child") as child:
            assert child.context.trace_id == root.context.trace_id
            assert child.context.span_id != root.context.span_id
            assert get_profiler().current_traceparent() == render_traceparent(child.context)
        assert get_profiler().current_traceparent() == render_traceparent(root.context)
    assert get_profiler().current_traceparent() is None
    with get_profiler().span("second root") as second_root:
        assert second_root.context.trace_id != root.context.trace_id
    events = drain_events()
    disable()

    assert [event["name"] for event in _events_of_phase(events, "X")] == ["child", "root", "second root"]
    assert all("trace_id" not in span["args"] for span in _events_of_phase(events, "X"))


def test_a_supplied_traceparent_sets_the_trace_and_a_malformed_one_is_ignored() -> None:
    header = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
    enable_collector()

    with get_profiler().span("request", traceparent=header) as request:
        assert request.context.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert request.context.span_id != "00f067aa0ba902b7"
        with get_profiler().span("nested", traceparent="garbage") as nested:
            assert nested.context.trace_id == request.context.trace_id
    with get_profiler().span("orphan", traceparent="garbage") as orphan:
        assert orphan.context.trace_id != "4bf92f3577b34da6a3ce929d0e0e4736"
    with get_profiler().span("outer") as outer:
        with get_profiler().span("handoff", traceparent=header) as handoff:
            assert handoff.context.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
            assert handoff.context.trace_id != outer.context.trace_id
        assert get_profiler().current_traceparent() == render_traceparent(outer.context)
    disable()


def test_a_span_whose_body_raises_is_recorded_as_failed_and_re_raises() -> None:
    enable_collector()
    with pytest.raises(_SampleProfilingError):
        with get_profiler().span("doomed", attempt=1):
            raise _SampleProfilingError("boom")
    assert get_profiler().current_traceparent() is None
    events = drain_events()
    disable()

    [doomed] = _events_of_phase(events, "X")
    assert doomed["name"] == "doomed"
    assert doomed["args"] == {"attempt": 1, "is_failed": True}
    assert doomed["dur"] >= 0


def test_collector_drops_events_past_its_cap_and_records_one_overflow_instant() -> None:
    collector = _make_collector(max_event_count=2)

    for name in ("a", "b", "c", "d"):
        with collector.span(name):
            pass
    first_drain = collector.drain()
    collector.instant("e")
    second_drain = collector.drain()

    assert [event["name"] for event in _events_of_phase(first_drain, "X")] == ["a", "b"]
    [overflow] = _events_of_phase(first_drain, "i")
    assert overflow["name"] == "overflow"
    assert overflow["cat"] == "profiling"
    assert overflow["args"] == {"max_event_count": 2}
    assert second_drain == []


def test_writer_drops_events_past_its_cap_and_records_one_overflow_instant(tmp_path: Path) -> None:
    writer = _make_writer(tmp_path / "trace.json", max_event_count=2, clock=_ManualClock(reading_us=0))
    writer.open()
    for name in ("a", "b", "c", "d"):
        writer.instant(name)
    writer.set_process_name("still named")
    writer.close()

    events = _read_trace_file(tmp_path / "trace.json")
    assert [event["name"] for event in _events_of_phase(events, "i")] == ["a", "b", "overflow"]
    assert _metadata_names(events, "process_name") == ["still named"]


def test_writer_flushes_to_disk_when_the_buffer_holds_512_events(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    writer = _make_writer(trace_path, max_event_count=10_000, clock=_ManualClock(reading_us=0))
    writer.open()

    # The first instant also buffers the thread's name, so 510 instants leave 511 events waiting.
    for _ in range(510):
        writer.instant("tick")
    assert _read_trace_file(trace_path) == []
    writer.instant("tick")

    flushed = _read_trace_file(trace_path)
    assert len(flushed) == 512
    assert len(_events_of_phase(flushed, "i")) == 511
    writer.close()
    assert len(_read_trace_file(trace_path)) == 512


def test_writer_flushes_to_disk_when_the_last_flush_is_older_than_one_second(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    clock = _ManualClock(reading_us=0)
    writer = _make_writer(trace_path, max_event_count=10_000, clock=clock)
    writer.open()

    writer.instant("first")
    clock.reading_us = 999_999
    writer.instant("second")
    assert _read_trace_file(trace_path) == []
    clock.reading_us = 1_000_000
    writer.instant("third")

    flushed = _read_trace_file(trace_path)
    assert [event["name"] for event in _events_of_phase(flushed, "i")] == ["first", "second", "third"]
    clock.reading_us = 1_999_999
    writer.instant("fourth")
    assert len(_events_of_phase(_read_trace_file(trace_path), "i")) == 3
    writer.close()


def test_writer_flush_writes_the_buffer_and_close_terminates_the_array(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    writer = _make_writer(trace_path, max_event_count=10_000, clock=_ManualClock(reading_us=0))
    writer.open()

    writer.instant("buffered")
    assert _read_trace_file(trace_path) == []
    writer.flush()
    assert [event["name"] for event in _events_of_phase(_read_trace_file(trace_path), "i")] == ["buffered"]
    assert not trace_path.read_text().rstrip().endswith("]")
    writer.instant("late")
    writer.close()
    writer.close()
    writer.instant("after close")
    writer.write_foreign_events([{"ph": "i", "name": "after close"}])
    writer.flush()

    events = json.loads(trace_path.read_text())
    assert [event["name"] for event in _events_of_phase(events, "i")] == ["buffered", "late"]


def test_disable_closes_the_writer_file_with_everything_buffered(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    enable_writer(trace_path, process_name="closing")
    get_profiler().instant("buffered")
    assert _read_trace_file(trace_path) == []

    disable()

    events = json.loads(trace_path.read_text())
    assert [event["name"] for event in _events_of_phase(events, "i")] == ["buffered"]


def test_write_foreign_events_appends_them_verbatim_without_counting_them(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    writer = _make_writer(trace_path, max_event_count=1, clock=_ManualClock(reading_us=0))
    writer.open()
    foreign_events = [
        {
            "ph": "X",
            "name": f"child-{idx}",
            "cat": "app",
            "ts": 10 + idx,
            "dur": 5,
            "pid": 424242,
            "tid": 7,
            "args": {},
        }
        for idx in range(3)
    ]

    writer.write_foreign_events(foreign_events)
    writer.instant("own")
    writer.instant("own too")
    writer.close()

    events = json.loads(trace_path.read_text())
    assert [event for event in events if event.get("pid") == 424242] == foreign_events
    assert [event["name"] for event in _events_of_phase(events, "i")] == ["own", "overflow"]


def test_write_foreign_events_counts_toward_the_flush_threshold(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.json"
    writer = _make_writer(trace_path, max_event_count=10, clock=_ManualClock(reading_us=0))
    writer.open()

    writer.write_foreign_events([{"ph": "i", "name": "foreign", "pid": 1, "tid": 1} for _ in range(600)])

    assert len(_read_trace_file(trace_path)) == 600
    writer.close()


def test_profiled_opens_a_span_on_the_live_profiler_at_call_time() -> None:
    assert _double(2) == 4
    enable_collector()
    assert _double(3) == 6
    events = drain_events()
    disable()

    [span] = _events_of_phase(events, "X")
    assert span["name"] == "decorated.double"
    assert span["cat"] == "math"
    assert span["args"] == {}
    assert _double.__name__ == "_double"


@profiled("decorated.double", "math")
def _double(value: int) -> int:
    return value * 2
