import pytest
from pydantic import ValidationError

from imbue.imbue_common.profiling import ClockSync
from imbue.minds.desktop_client.profile_ingestion import CHROMIUM_TRACE_ADAPTER
from imbue.minds.desktop_client.profile_ingestion import ChromiumTraceDocument
from imbue.minds.desktop_client.profile_ingestion import PageSessionProcess
from imbue.minds.desktop_client.profile_ingestion import PageSessionRegistry
from imbue.minds.desktop_client.profile_ingestion import chromium_trace_events
from imbue.minds.desktop_client.profile_ingestion import page_batch_to_trace_events
from imbue.minds.desktop_client.profile_ingestion import page_process_name
from imbue.minds.desktop_client.profile_ingestion import scrub_chromium_event
from imbue.minds.desktop_client.profile_ingestion import scrub_url_query_strings
from imbue.minds.desktop_client.ui_models import UiProfileEvent
from imbue.minds.desktop_client.ui_models import UiProfileEventKind
from imbue.minds.desktop_client.ui_models import UiProfileEventsBatch

# A sync point at which the wall clock read 1,700,000,000 s and the trace clock 5 s.
_CLOCK_SYNC = ClockSync(epoch_ns=1_700_000_000_000_000_000, perf_counter_ns=5_000_000_000)
# A page whose time origin is 2 s after the sync point.
_TIME_ORIGIN_MS = 1_700_000_002_000.0


def _span(name: str, start_ms: float, duration_ms: float, **extra: object) -> UiProfileEvent:
    return UiProfileEvent.model_validate(
        {"kind": "span", "name": name, "cat": "ui", "start_ms": start_ms, "duration_ms": duration_ms, **extra}
    )


def test_page_sessions_get_synthetic_pids_in_order_of_first_appearance() -> None:
    registry = PageSessionRegistry()

    first = registry.process_for("session-a")
    second = registry.process_for("session-b")
    first_again = registry.process_for("session-a")

    assert first == PageSessionProcess(pid=1_000_001, ordinal=1, is_first_batch=True)
    assert second == PageSessionProcess(pid=1_000_002, ordinal=2, is_first_batch=True)
    assert first_again == PageSessionProcess(pid=1_000_001, ordinal=1, is_first_batch=False)


def test_page_process_name_carries_the_renderer_pid_when_the_shell_reported_one() -> None:
    assert page_process_name(1, None) == "Imbue Studio UI 1"
    assert page_process_name(2, 4242) == "Imbue Studio UI 2 (renderer pid 4242)"


def test_page_batch_converts_spans_and_instants_onto_the_trace_clock_with_their_flows() -> None:
    batch = UiProfileEventsBatch(
        session_id="session-a",
        time_origin_ms=_TIME_ORIGIN_MS,
        renderer_pid=4242,
        events=(
            _span(
                "fetch GET /ui/api/app-status", 100.25, 12.5004, args={"status": 200}, flow_out_id="00f067aa0ba902b7"
            ),
            _span("sharePanel.load", 90.0, 30.0, flow_in_id="abc"),
            UiProfileEvent(kind=UiProfileEventKind.INSTANT, name="pagehide", cat="ui", start_ms=500.0, duration_ms=0),
        ),
    )

    events = page_batch_to_trace_events(
        batch, PageSessionProcess(pid=1_000_001, ordinal=1, is_first_batch=True), _CLOCK_SYNC
    )

    # 5 s on the trace clock, plus the 2 s to the time origin, plus the event's own offset.
    assert events == [
        {
            "ph": "M",
            "name": "process_name",
            "pid": 1_000_001,
            "tid": 1,
            "args": {"name": "Imbue Studio UI 1 (renderer pid 4242)"},
        },
        {"ph": "M", "name": "thread_name", "pid": 1_000_001, "tid": 1, "args": {"name": "main"}},
        {
            "ph": "X",
            "name": "fetch GET /ui/api/app-status",
            "cat": "ui",
            "ts": 7_100_250,
            "dur": 12_500,
            "pid": 1_000_001,
            "tid": 1,
            "args": {"status": 200},
            "bind_id": "00f067aa0ba902b7",
            "flow_out": True,
        },
        {
            "ph": "X",
            "name": "sharePanel.load",
            "cat": "ui",
            "ts": 7_090_000,
            "dur": 30_000,
            "pid": 1_000_001,
            "tid": 1,
            "args": {},
            "bind_id": "abc",
            "flow_in": True,
        },
        {
            "ph": "i",
            "s": "t",
            "name": "pagehide",
            "cat": "ui",
            "ts": 7_500_000,
            "pid": 1_000_001,
            "tid": 1,
            "args": {},
        },
    ]


def test_a_later_batch_of_a_session_carries_no_metadata_events() -> None:
    batch = UiProfileEventsBatch(session_id="session-a", time_origin_ms=_TIME_ORIGIN_MS, events=(_span("x", 0, 1),))

    events = page_batch_to_trace_events(
        batch, PageSessionProcess(pid=1_000_001, ordinal=1, is_first_batch=False), _CLOCK_SYNC
    )

    assert [event["ph"] for event in events] == ["X"]


def test_a_page_event_naming_two_different_flows_is_refused() -> None:
    with pytest.raises(ValidationError):
        _span("torn", 0, 1, flow_out_id="a", flow_in_id="b")
    assert _span("relay", 0, 1, flow_out_id="a", flow_in_id="a").flow_in_id == "a"


def test_scrub_url_query_strings_cuts_the_query_and_fragment_of_every_url_however_nested() -> None:
    scrubbed = scrub_url_query_strings(
        {
            "url": "https://studio.example/login?one_time_code=SECRET#frag",
            "nested": {"list": ["http://a.example/?x=1", "file:///tmp/x?y=2", "not a url?x=1", 3, None]},
            "data": {"priority": "high", "fragment_only": "https://a.example/path#section"},
        }
    )

    assert scrubbed == {
        "url": "https://studio.example/login",
        "nested": {"list": ["http://a.example/", "file:///tmp/x", "not a url?x=1", 3, None]},
        "data": {"priority": "high", "fragment_only": "https://a.example/path"},
    }


def test_scrub_chromium_event_touches_only_the_args() -> None:
    event = {
        "ph": "X",
        "name": "ResourceSendRequest?keep=me",
        "cat": "devtools.timeline",
        "ts": 1,
        "dur": 2,
        "pid": 7,
        "tid": 8,
        "args": {"data": {"url": "https://studio.example/ui/?accent=%23fff", "requestMethod": "GET"}},
    }

    scrubbed = scrub_chromium_event(event)

    assert scrubbed == {**event, "args": {"data": {"url": "https://studio.example/ui/", "requestMethod": "GET"}}}
    assert event["args"]["data"]["url"] == "https://studio.example/ui/?accent=%23fff"


def test_chromium_trace_events_are_read_from_the_object_form_and_the_bare_array_dropping_metadata() -> None:
    object_form = CHROMIUM_TRACE_ADAPTER.validate_json(
        b'{"traceEvents": [{"ph": "M", "name": "process_name", "pid": 1}], "metadata": {"trace-config": "{}"}}'
    )
    bare_array = CHROMIUM_TRACE_ADAPTER.validate_json(b'[{"ph": "i", "name": "x", "pid": 1}]')

    assert isinstance(object_form, ChromiumTraceDocument)
    assert chromium_trace_events(object_form) == [{"ph": "M", "name": "process_name", "pid": 1}]
    assert chromium_trace_events(bare_array) == [{"ph": "i", "name": "x", "pid": 1}]
    with pytest.raises(ValidationError):
        CHROMIUM_TRACE_ADAPTER.validate_json(b'{"metadata": {}}')
    with pytest.raises(ValidationError):
        CHROMIUM_TRACE_ADAPTER.validate_json(b'"just a string"')
