import inspect
import json
import re
from pathlib import Path

import pytest

from imbue.imbue_common.profiling import get_profiler
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.request_profiling import STREAMING_RULES
from imbue.minds.desktop_client.request_profiling import is_rule_profiled
from imbue.minds.desktop_client.testing import flushed_trace_events
from imbue.minds.desktop_client.testing import trace_spans_named

_PAGE_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def test_a_request_records_one_span_named_by_its_rule_as_the_target_of_the_pages_flow(
    tmp_path: Path, profile_writer_path: Path
) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/app-status?probe=secret", headers={"traceparent": _PAGE_TRACEPARENT})

    assert response.status_code == 200
    [span] = trace_spans_named(flushed_trace_events(profile_writer_path), "GET /ui/api/app-status")
    assert span["args"]["status"] == 200
    assert span["args"]["path"] == "/ui/api/app-status"
    assert span["args"]["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert span["args"]["parent_span_id"] == "00f067aa0ba902b7"
    assert _SPAN_ID_PATTERN.match(span["args"]["span_id"])
    assert span["args"]["span_id"] != "00f067aa0ba902b7"
    # The page's request span is the flow source with its own span id, so this span is the target of that id.
    assert span["bind_id"] == "00f067aa0ba902b7"
    assert span["flow_in"] is True
    assert "flow_out" not in span
    assert "secret" not in json.dumps(span)


def test_a_request_without_the_header_gets_a_minted_trace_id_and_joins_no_flow(
    tmp_path: Path, profile_writer_path: Path
) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    client.get("/ui/api/app-status")
    client.get("/ui/api/app-status", headers={"traceparent": "not a header"})

    spans = trace_spans_named(flushed_trace_events(profile_writer_path), "GET /ui/api/app-status")
    assert len(spans) == 2
    for span in spans:
        assert _TRACE_ID_PATTERN.match(span["args"]["trace_id"])
        assert "parent_span_id" not in span["args"]
        assert not {"bind_id", "flow_in", "flow_out"} & span.keys()
    assert spans[0]["args"]["trace_id"] != spans[1]["args"]["trace_id"]


def test_a_request_no_rule_matches_is_named_unmatched_with_its_status(
    tmp_path: Path, profile_writer_path: Path
) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/no/such/page")

    assert response.status_code == 404
    [span] = trace_spans_named(flushed_trace_events(profile_writer_path), "GET <unmatched>")
    assert span["args"]["status"] == 404
    assert span["args"]["path"] == "/no/such/page"


def test_a_streaming_route_records_no_span(tmp_path: Path, profile_writer_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, paths=InstallationPaths(data_dir=tmp_path / "minds")
    )

    response = client.get("/api/v1/workspaces/operations/create/op-1/logs")

    # The handler's own 404 for an unknown operation: the rule matched, so an unmatched span would show here.
    assert response.status_code == 404
    events = flushed_trace_events(profile_writer_path)
    assert [event for event in events if event["ph"] == "X" and event["name"].startswith("GET ")] == []


def test_hooks_are_inert_while_profiling_is_off(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/app-status", headers={"traceparent": _PAGE_TRACEPARENT})

    assert response.status_code == 200
    assert get_profiler().is_enabled is False


@pytest.mark.parametrize(
    ("rule", "is_profiled"),
    [
        ("/ui/api/app-status", True),
        ("/api/v1/workspace-sharing/<workspace_id>", True),
        ("<unmatched>", True),
        ("/ui/ws", False),
        ("/api/v1/workspaces/operations/create/<operation_id>/logs", False),
        ("/ui/api/profile/events", False),
        ("/ui/api/profile/chromium", False),
    ],
)
def test_is_rule_profiled_excludes_the_streams_and_the_profilers_own_endpoints(rule: str, is_profiled: bool) -> None:
    assert is_rule_profiled(rule) is is_profiled


def test_streaming_rules_name_exactly_the_routes_that_stream(tmp_path: Path) -> None:
    """The exclusion list is spelled by hand, so it is checked against the routes' own sources."""
    _client, app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=False, paths=InstallationPaths(data_dir=tmp_path / "minds")
    )

    streaming_rules = {
        rule.rule
        for rule in app.url_map.iter_rules()
        if rule.websocket
        or "text/event-stream" in inspect.getsource(inspect.unwrap(app.view_functions[rule.endpoint]))
    }

    assert streaming_rules == STREAMING_RULES
