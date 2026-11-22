import re
from pathlib import Path

import pytest

from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.request_profiling import STREAMING_RULES
from imbue.minds.desktop_client.request_timing import is_rule_timed
from imbue.mngr.utils.testing import capture_loguru

_TIMING_LINE_PATTERN = re.compile(r"^(?P<method>[A-Z]+) (?P<path>\S+) -> (?P<outcome>.+) in (?P<ms>\d+) ms$")


def _timing_lines(log_text: str) -> list[dict[str, str]]:
    matches = (_TIMING_LINE_PATTERN.match(line) for line in log_text.splitlines())
    return [match.groupdict() for match in matches if match is not None]


def test_an_api_request_logs_its_method_path_status_and_duration_at_debug(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    with capture_loguru(level="DEBUG") as log_output:
        response = client.get("/ui/api/app-status?probe=secret")

    assert response.status_code == 200
    [line] = _timing_lines(log_output.getvalue())
    assert (line["method"], line["path"], line["outcome"]) == ("GET", "/ui/api/app-status", "200")
    assert "secret" not in log_output.getvalue()


def test_a_refused_api_request_logs_the_status_it_was_refused_with(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    with capture_loguru(level="DEBUG") as log_output:
        response = client.get("/ui/api/workspaces/agent-0000/options")

    [line] = _timing_lines(log_output.getvalue())
    assert line["path"] == "/ui/api/workspaces/agent-0000/options"
    assert line["outcome"] == str(response.status_code)


def test_a_page_request_logs_nothing(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    with capture_loguru(level="DEBUG") as log_output:
        client.get("/settings")

    assert _timing_lines(log_output.getvalue()) == []


@pytest.mark.parametrize("rule", sorted(STREAMING_RULES))
def test_a_stream_that_lasts_as_long_as_its_operation_is_not_timed(rule: str) -> None:
    assert not is_rule_timed(rule)


@pytest.mark.parametrize(
    ("rule", "is_timed"),
    [
        ("/api/v1/workspace-sharing/<workspace_id>", True),
        ("/ui/api/settings", True),
        ("/ui/api/profile/events", False),
        ("/settings", False),
        ("/<path:path>", False),
    ],
)
def test_only_api_routes_are_timed(rule: str, is_timed: bool) -> None:
    assert is_rule_timed(rule) is is_timed
