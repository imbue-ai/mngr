import json
from pathlib import Path
from typing import Any

from flask.testing import FlaskClient

from imbue.imbue_common.profiling import get_profiler
from imbue.imbue_common.profiling import rebase_epoch_us
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.testing import flushed_trace_events
from imbue.minds.desktop_client.ui_api_profile import MAX_CHROMIUM_BODY_BYTES
from imbue.minds.desktop_client.ui_api_profile import MAX_EVENTS_BODY_BYTES
from imbue.minds.desktop_client.ui_api_profile import _read_body_within

_TIME_ORIGIN_MS = 1_700_000_002_000.0


def _batch(session_id: str, events: list[dict[str, Any]], renderer_pid: int | None = None) -> str:
    return json.dumps(
        {"session_id": session_id, "time_origin_ms": _TIME_ORIGIN_MS, "renderer_pid": renderer_pid, "events": events}
    )


def _span(name: str, start_ms: float, duration_ms: float, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "span",
        "name": name,
        "cat": "ui",
        "start_ms": start_ms,
        "duration_ms": duration_ms,
        "args": {},
        "flow_out_id": None,
        "flow_in_id": None,
        **extra,
    }


def _post_events(client: FlaskClient, body: str, content_type: str = "application/json") -> int:
    return client.post("/ui/api/profile/events", data=body, content_type=content_type).status_code


def test_the_profile_endpoints_do_not_exist_while_profiling_is_off(tmp_path: Path) -> None:
    client, app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert not any(rule.startswith("/ui/api/profile/") for rule in rules)
    assert _post_events(client, _batch("s", [])) == 404
    assert client.post("/ui/api/profile/chromium", data="[]", content_type="application/json").status_code == 404


def test_the_profile_endpoints_require_the_session_cookie(tmp_path: Path, profile_writer_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    assert _post_events(client, _batch("s", [])) == 401
    assert client.post("/ui/api/profile/chromium", data="[]", content_type="application/json").status_code == 401
    events = flushed_trace_events(profile_writer_path)
    assert [event for event in events if event.get("pid", 0) >= 1_000_000] == []
    assert [event for event in events if event["ph"] == "X" and event["name"].startswith("POST ")] == []


def test_an_oversized_body_is_refused_before_it_is_read(tmp_path: Path, profile_writer_path: Path) -> None:
    client, app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    assert _post_events(client, "x" * (MAX_EVENTS_BODY_BYTES + 1)) == 413
    assert _post_events(client, "x" * MAX_EVENTS_BODY_BYTES) == 400
    # The Chromium limit is too large to post in a test; the reader refuses on the declared length alone.
    with app.test_request_context(
        "/ui/api/profile/chromium",
        method="POST",
        data=b"[]",
        environ_overrides={"CONTENT_LENGTH": str(MAX_CHROMIUM_BODY_BYTES + 1)},
    ):
        assert _read_body_within(MAX_CHROMIUM_BODY_BYTES) is None
    with app.test_request_context("/ui/api/profile/chromium", method="POST", data=b"[]"):
        assert _read_body_within(MAX_CHROMIUM_BODY_BYTES) == b"[]"
    with app.test_request_context("/ui/api/profile/chromium", method="POST", data=b"[1, 2]"):
        assert _read_body_within(3) is None


def test_a_malformed_events_body_is_refused(tmp_path: Path, profile_writer_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    assert _post_events(client, "not json") == 400
    assert _post_events(client, json.dumps({"session_id": "s", "events": []})) == 400
    assert _post_events(client, _batch("s", [{"kind": "wave", "name": "x"}])) == 400


def test_posted_page_events_land_in_the_file_under_one_synthetic_process_per_session(
    tmp_path: Path, profile_writer_path: Path
) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)
    first_batch = _batch(
        "session-a",
        [
            _span("fetch GET /ui/api/app-status", 100.25, 12.5, args={"status": 200}, flow_out_id="00f067aa0ba902b7"),
            {"kind": "instant", "name": "pagehide", "cat": "ui", "start_ms": 500, "duration_ms": 0, "args": {}},
        ],
        renderer_pid=4242,
    )

    # The first batch of a session arrives as application/json; a pagehide beacon arrives as text/plain.
    assert _post_events(client, first_batch) == 204
    assert _post_events(client, _batch("session-b", [_span("other window", 1, 2)]), "text/plain") == 204
    assert _post_events(client, _batch("session-a", [_span("later", 600, 1, flow_in_id="abc")]), "text/plain") == 204

    events = flushed_trace_events(profile_writer_path)
    page_events = [event for event in events if event.get("pid", 0) >= 1_000_000]
    expected_ts = rebase_epoch_us((_TIME_ORIGIN_MS + 100.25) * 1000, get_profiler().clock_sync)
    assert page_events == [
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
            "ts": expected_ts,
            "dur": 12_500,
            "pid": 1_000_001,
            "tid": 1,
            "args": {"status": 200},
            "bind_id": "00f067aa0ba902b7",
            "flow_out": True,
        },
        {
            "ph": "i",
            "s": "t",
            "name": "pagehide",
            "cat": "ui",
            "ts": expected_ts + 399_750,
            "pid": 1_000_001,
            "tid": 1,
            "args": {},
        },
        {"ph": "M", "name": "process_name", "pid": 1_000_002, "tid": 1, "args": {"name": "Imbue Studio UI 2"}},
        {"ph": "M", "name": "thread_name", "pid": 1_000_002, "tid": 1, "args": {"name": "main"}},
        {
            "ph": "X",
            "name": "other window",
            "cat": "ui",
            "ts": expected_ts - 99_250,
            "dur": 2_000,
            "pid": 1_000_002,
            "tid": 1,
            "args": {},
        },
        {
            "ph": "X",
            "name": "later",
            "cat": "ui",
            "ts": expected_ts + 499_750,
            "dur": 1_000,
            "pid": 1_000_001,
            "tid": 1,
            "args": {},
            "bind_id": "abc",
            "flow_in": True,
        },
    ]
    # The posts themselves are not recorded.
    assert [event for event in events if event["ph"] == "X" and event["name"].startswith("POST ")] == []


def test_a_posted_chromium_trace_is_written_verbatim_except_for_url_query_strings_and_is_flushed_at_once(
    tmp_path: Path, profile_writer_path: Path
) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)
    resource_event = {
        "ph": "X",
        "name": "ResourceSendRequest",
        "cat": "devtools.timeline",
        "ts": 123,
        "dur": 4,
        "pid": 31337,
        "tid": 2,
        "args": {"data": {"url": "https://studio.example/login?one_time_code=SECRET", "requestMethod": "GET"}},
    }
    browser_metadata = {"ph": "M", "name": "process_name", "pid": 31337, "tid": 2, "args": {"name": "Browser"}}
    document = {"traceEvents": [browser_metadata, resource_event], "metadata": {"trace-config": "{}"}}

    response = client.post("/ui/api/profile/chromium", data=json.dumps(document), content_type="application/json")

    assert response.status_code == 204
    # Flushed by the endpoint itself: the file holds the events before anything else flushes the writer.
    text_on_disk = profile_writer_path.read_text()
    assert "ResourceSendRequest" in text_on_disk
    assert "SECRET" not in text_on_disk
    assert "trace-config" not in text_on_disk
    chromium_events = [event for event in flushed_trace_events(profile_writer_path) if event.get("pid") == 31337]
    assert chromium_events == [
        browser_metadata,
        {**resource_event, "args": {"data": {"url": "https://studio.example/login", "requestMethod": "GET"}}},
    ]


def test_a_chromium_trace_posted_as_a_bare_array_is_accepted(tmp_path: Path, profile_writer_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)
    events = [{"ph": "i", "s": "t", "name": "marker", "cat": "toplevel", "ts": 5, "pid": 31338, "tid": 1, "args": {}}]

    response = client.post("/ui/api/profile/chromium", data=json.dumps(events), content_type="application/json")

    assert response.status_code == 204
    assert [event for event in flushed_trace_events(profile_writer_path) if event.get("pid") == 31338] == events
    assert client.post("/ui/api/profile/chromium", data="{}", content_type="application/json").status_code == 400
