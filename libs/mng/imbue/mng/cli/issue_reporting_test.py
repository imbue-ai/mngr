import json
from uuid import uuid4

import pytest
from inline_snapshot import snapshot

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mng.cli.issue_reporting import ExistingIssue
from imbue.mng.cli.issue_reporting import GITHUB_BASE_URL
from imbue.mng.cli.issue_reporting import build_issue_body
from imbue.mng.cli.issue_reporting import build_issue_title
from imbue.mng.cli.issue_reporting import build_new_issue_url
from imbue.mng.cli.issue_reporting import handle_not_implemented_error
from imbue.mng.cli.issue_reporting import search_for_existing_issue


def _fake_finished_process(returncode: int, stdout: str, command: tuple[str, ...] = ("fake",)) -> FinishedProcess:
    """Create a FinishedProcess for test mocking."""
    return FinishedProcess(
        returncode=returncode,
        stdout=stdout,
        stderr="",
        command=command,
        is_output_already_logged=False,
    )


# =============================================================================
# Tests for build_issue_title
# =============================================================================


def test_build_issue_title_simple_message() -> None:
    title = build_issue_title("--sync-mode=full is not implemented yet")
    assert title == snapshot("[NotImplemented] --sync-mode=full is not implemented yet")


def test_build_issue_title_multiline_uses_first_line() -> None:
    title = build_issue_title("Feature X\nSome additional details\nMore info")
    assert title == snapshot("[NotImplemented] Feature X")


def test_build_issue_title_strips_whitespace() -> None:
    title = build_issue_title("  some error message  ")
    assert title == snapshot("[NotImplemented] some error message")


def test_build_issue_title_empty_message() -> None:
    title = build_issue_title("")
    assert title == snapshot("[NotImplemented] ")


# =============================================================================
# Tests for build_issue_body
# =============================================================================


def test_build_issue_body_contains_error_message() -> None:
    body = build_issue_body("--exclude is not implemented yet")
    assert "--exclude is not implemented yet" in body
    assert "Feature Request" in body
    assert "Use Case" in body


def test_build_issue_body_wraps_message_in_code_block() -> None:
    body = build_issue_body("some error")
    assert "```\nsome error\n```" in body


# =============================================================================
# Tests for build_new_issue_url
# =============================================================================


def test_build_new_issue_url_contains_base_url() -> None:
    url = build_new_issue_url("test title", "test body")
    assert url.startswith(f"{GITHUB_BASE_URL}/issues/new?")


def test_build_new_issue_url_encodes_title_and_body() -> None:
    url = build_new_issue_url("[NotImplemented] --sync-mode=full", "Body\nDetails")
    assert "title=" in url
    assert "body=" in url
    # Spaces and special chars should be encoded
    assert " " not in url.split("?")[1]


def test_build_new_issue_url_truncates_long_body() -> None:
    long_body = "x" * 10000
    url = build_new_issue_url("title", long_body)
    assert len(url) <= 8000


# =============================================================================
# Tests for search_for_existing_issue
# =============================================================================


def test_search_for_existing_issue_returns_none_when_both_fail(
    cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both GitHub API and gh CLI fail, search returns None.

    Points at a nonexistent GitHub repo so both curl and gh CLI fail
    with real errors rather than mocking the failure.
    """
    fake_repo = f"nonexistent-org/nonexistent-repo-{uuid4().hex}"
    monkeypatch.setattr("imbue.mng.cli.issue_reporting.GITHUB_REPO", fake_repo)

    result = search_for_existing_issue("some error", cg)
    assert result is None


def test_search_for_existing_issue_falls_back_to_gh_cli(monkeypatch: pytest.MonkeyPatch, cg: ConcurrencyGroup) -> None:
    """When GitHub API fails, search falls back to gh CLI."""
    call_count = 0

    def fake_run(self, command, **kwargs):
        nonlocal call_count
        call_count += 1
        if command[0] == "curl":
            return _fake_finished_process(returncode=22, stdout="", command=tuple(command))
        else:
            return _fake_finished_process(
                returncode=0,
                stdout=json.dumps(
                    [{"number": 42, "title": "[NotImplemented] test feature", "url": "https://github.com/test/42"}]
                ),
                command=tuple(command),
            )

    monkeypatch.setattr(ConcurrencyGroup, "run_process_to_completion", fake_run)

    result = search_for_existing_issue("test feature", cg)
    assert result is not None
    assert result.number == 42
    assert result.url == "https://github.com/test/42"
    # Should have called both curl (failed) and gh (succeeded)
    assert call_count == 2


def test_search_for_existing_issue_returns_api_result_when_found(
    cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When GitHub API finds an issue, returns it without trying gh CLI."""
    call_count = 0

    def fake_run(self, command, **kwargs):
        nonlocal call_count
        call_count += 1
        return _fake_finished_process(
            returncode=0,
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "number": 99,
                            "title": "[NotImplemented] --sync-mode=full",
                            "html_url": "https://github.com/imbue-ai/mng/issues/99",
                        }
                    ]
                }
            ),
            command=tuple(command),
        )

    monkeypatch.setattr(ConcurrencyGroup, "run_process_to_completion", fake_run)

    result = search_for_existing_issue("--sync-mode=full", cg)
    assert result is not None
    assert result.number == 99
    assert result.url == "https://github.com/imbue-ai/mng/issues/99"
    # Should only call curl (API), not gh
    assert call_count == 1


def test_search_for_existing_issue_returns_none_when_no_results(
    cg: ConcurrencyGroup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When API returns empty results, returns None."""

    def fake_run(self, command, **kwargs):
        return _fake_finished_process(
            returncode=0,
            stdout=json.dumps({"items": []}),
            command=tuple(command),
        )

    monkeypatch.setattr(ConcurrencyGroup, "run_process_to_completion", fake_run)

    result = search_for_existing_issue("nonexistent feature", cg)
    assert result is None


# =============================================================================
# Tests for handle_not_implemented_error
# =============================================================================


def test_handle_not_implemented_error_non_interactive_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    """In non-interactive mode, handle_not_implemented_error logs error and exits."""
    monkeypatch.setattr("imbue.mng.cli.issue_reporting.sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit) as exc_info:
        handle_not_implemented_error(NotImplementedError("--sync-mode=full is not implemented yet"))

    assert exc_info.value.code == 1


def test_handle_not_implemented_error_interactive_declined(monkeypatch: pytest.MonkeyPatch) -> None:
    """In interactive mode, if user declines to report, just exits."""
    monkeypatch.setattr("imbue.mng.cli.issue_reporting.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("imbue.mng.cli.issue_reporting.click.confirm", lambda *args, **kwargs: False)

    with pytest.raises(SystemExit) as exc_info:
        handle_not_implemented_error(NotImplementedError("some feature"))

    assert exc_info.value.code == 1


def test_handle_not_implemented_error_empty_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """NotImplementedError with no message uses a default."""
    monkeypatch.setattr("imbue.mng.cli.issue_reporting.sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit):
        handle_not_implemented_error(NotImplementedError())


def test_handle_not_implemented_error_interactive_opens_existing_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """In interactive mode with existing issue found, opens its URL."""
    monkeypatch.setattr("imbue.mng.cli.issue_reporting.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("imbue.mng.cli.issue_reporting.click.confirm", lambda *args, **kwargs: True)

    # Return an existing issue from the API search
    def fake_run(self, command, **kwargs):
        return _fake_finished_process(
            returncode=0,
            stdout=json.dumps(
                {
                    "items": [
                        {
                            "number": 77,
                            "title": "[NotImplemented] --sync-mode=full",
                            "html_url": "https://github.com/imbue-ai/mng/issues/77",
                        }
                    ]
                }
            ),
            command=tuple(command),
        )

    monkeypatch.setattr(ConcurrencyGroup, "run_process_to_completion", fake_run)

    opened_urls: list[str] = []
    monkeypatch.setattr("imbue.mng.cli.issue_reporting.webbrowser.open", lambda url: opened_urls.append(url))

    with pytest.raises(SystemExit):
        handle_not_implemented_error(NotImplementedError("--sync-mode=full is not implemented yet"))

    assert len(opened_urls) == 1
    assert opened_urls[0] == "https://github.com/imbue-ai/mng/issues/77"


def test_handle_not_implemented_error_interactive_opens_new_issue_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """In interactive mode with no existing issue, opens new issue form."""
    monkeypatch.setattr("imbue.mng.cli.issue_reporting.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("imbue.mng.cli.issue_reporting.click.confirm", lambda *args, **kwargs: True)

    # Return empty results from API, then also empty from gh CLI
    def fake_run(self, command, **kwargs):
        if command[0] == "curl":
            return _fake_finished_process(returncode=0, stdout=json.dumps({"items": []}), command=tuple(command))
        else:
            return _fake_finished_process(returncode=0, stdout=json.dumps([]), command=tuple(command))

    monkeypatch.setattr(ConcurrencyGroup, "run_process_to_completion", fake_run)

    opened_urls: list[str] = []
    monkeypatch.setattr("imbue.mng.cli.issue_reporting.webbrowser.open", lambda url: opened_urls.append(url))

    with pytest.raises(SystemExit):
        handle_not_implemented_error(NotImplementedError("--exclude is not implemented yet"))

    assert len(opened_urls) == 1
    assert opened_urls[0].startswith(f"{GITHUB_BASE_URL}/issues/new?")
    assert "NotImplemented" in opened_urls[0]


# =============================================================================
# Tests for ExistingIssue
# =============================================================================


def test_existing_issue_is_frozen() -> None:
    issue = ExistingIssue(number=1, title="test", url="https://example.com")
    assert issue.model_config.get("frozen") is True
