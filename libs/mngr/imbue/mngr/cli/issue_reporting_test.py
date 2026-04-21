import json
from pathlib import Path
from uuid import uuid4

import click
import pytest
from inline_snapshot import snapshot

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr.cli.issue_reporting import DIAGNOSE_FLOW_META_KEY
from imbue.mngr.cli.issue_reporting import ExistingIssue
from imbue.mngr.cli.issue_reporting import GITHUB_BASE_URL
from imbue.mngr.cli.issue_reporting import UNEXPECTED_ERROR_TITLE_PREFIX
from imbue.mngr.cli.issue_reporting import _is_diagnose_command
from imbue.mngr.cli.issue_reporting import _offer_diagnose
from imbue.mngr.cli.issue_reporting import build_issue_body
from imbue.mngr.cli.issue_reporting import build_issue_title
from imbue.mngr.cli.issue_reporting import build_new_issue_url
from imbue.mngr.cli.issue_reporting import build_unexpected_error_issue_body
from imbue.mngr.cli.issue_reporting import build_unexpected_error_issue_title
from imbue.mngr.cli.issue_reporting import handle_not_implemented_error
from imbue.mngr.cli.issue_reporting import handle_unexpected_error
from imbue.mngr.cli.issue_reporting import search_for_existing_issue
from imbue.mngr.cli.issue_reporting import write_diagnose_context_file
from imbue.mngr.main import cli
from imbue.mngr.utils.testing import capture_loguru


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
    monkeypatch.setattr("imbue.mngr.cli.issue_reporting.GITHUB_REPO", fake_repo)

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
                            "html_url": "https://github.com/imbue-ai/mngr/issues/99",
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
    assert result.url == "https://github.com/imbue-ai/mngr/issues/99"
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
    monkeypatch.setattr("imbue.mngr.cli.issue_reporting.sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit) as exc_info:
        handle_not_implemented_error(NotImplementedError("--sync-mode=full is not implemented yet"))

    assert exc_info.value.code == 1


def test_handle_not_implemented_error_interactive_declined(monkeypatch: pytest.MonkeyPatch) -> None:
    """In interactive mode, if user declines to report, just exits."""
    monkeypatch.setattr("imbue.mngr.cli.issue_reporting.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("imbue.mngr.cli.issue_reporting.click.confirm", lambda *args, **kwargs: False)

    with pytest.raises(SystemExit) as exc_info:
        handle_not_implemented_error(NotImplementedError("some feature"))

    assert exc_info.value.code == 1


def test_handle_not_implemented_error_empty_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """NotImplementedError with no message uses a default."""
    monkeypatch.setattr("imbue.mngr.cli.issue_reporting.sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit):
        handle_not_implemented_error(NotImplementedError())


def test_handle_not_implemented_error_interactive_opens_existing_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """In interactive mode with existing issue found, opens its URL."""
    monkeypatch.setattr("imbue.mngr.cli.issue_reporting.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("imbue.mngr.cli.issue_reporting.click.confirm", lambda *args, **kwargs: True)

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
                            "html_url": "https://github.com/imbue-ai/mngr/issues/77",
                        }
                    ]
                }
            ),
            command=tuple(command),
        )

    monkeypatch.setattr(ConcurrencyGroup, "run_process_to_completion", fake_run)

    opened_urls: list[str] = []
    monkeypatch.setattr("imbue.mngr.cli.issue_reporting.webbrowser.open", lambda url: opened_urls.append(url))

    with pytest.raises(SystemExit):
        handle_not_implemented_error(NotImplementedError("--sync-mode=full is not implemented yet"))

    assert len(opened_urls) == 1
    assert opened_urls[0] == "https://github.com/imbue-ai/mngr/issues/77"


def test_handle_not_implemented_error_interactive_opens_new_issue_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """In interactive mode with no existing issue, opens new issue form."""
    monkeypatch.setattr("imbue.mngr.cli.issue_reporting.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("imbue.mngr.cli.issue_reporting.click.confirm", lambda *args, **kwargs: True)

    # Return empty results from API, then also empty from gh CLI
    def fake_run(self, command, **kwargs):
        if command[0] == "curl":
            return _fake_finished_process(returncode=0, stdout=json.dumps({"items": []}), command=tuple(command))
        else:
            return _fake_finished_process(returncode=0, stdout=json.dumps([]), command=tuple(command))

    monkeypatch.setattr(ConcurrencyGroup, "run_process_to_completion", fake_run)

    opened_urls: list[str] = []
    monkeypatch.setattr("imbue.mngr.cli.issue_reporting.webbrowser.open", lambda url: opened_urls.append(url))

    with pytest.raises(SystemExit):
        handle_not_implemented_error(NotImplementedError("--exclude is not implemented yet"))

    assert len(opened_urls) == 1
    assert opened_urls[0].startswith(f"{GITHUB_BASE_URL}/issues/new?")
    assert "NotImplemented" in opened_urls[0]


# =============================================================================
# Tests for is_interactive parameter on error handlers
# =============================================================================


def test_handle_not_implemented_error_is_interactive_false_exits_without_prompting() -> None:
    """When is_interactive=False is explicitly passed, exits without prompting regardless of TTY state.

    This verifies that the is_interactive parameter takes precedence over the fallback
    sys.stdin.isatty() check. The is_interactive=True path is already tested by the existing
    tests above that monkeypatch sys.stdin.isatty to return True.
    """
    with pytest.raises(SystemExit) as exc_info:
        handle_not_implemented_error(NotImplementedError("some feature"), is_interactive=False)

    assert exc_info.value.code == 1


def test_handle_unexpected_error_is_interactive_false_exits_without_prompting() -> None:
    """When is_interactive=False is explicitly passed, exits without prompting regardless of TTY state.

    This verifies that the is_interactive parameter takes precedence over the fallback
    sys.stdin.isatty() check. The is_interactive=True path is already tested by the existing
    tests for handle_not_implemented_error.
    """
    with pytest.raises(SystemExit) as exc_info:
        handle_unexpected_error(RuntimeError("boom"), is_interactive=False)

    assert exc_info.value.code == 1


# =============================================================================
# Tests for ExistingIssue
# =============================================================================


def test_existing_issue_is_frozen() -> None:
    issue = ExistingIssue(number=1, title="test", url="https://example.com")
    assert issue.model_config.get("frozen") is True


# =============================================================================
# Tests for build_unexpected_error_issue_title and build_unexpected_error_issue_body
# =============================================================================


def test_build_unexpected_error_issue_title_includes_error_type_and_message() -> None:
    """build_unexpected_error_issue_title should include the error type and first line of message."""
    error = ValueError("Something went wrong\nSecond line details")
    title = build_unexpected_error_issue_title(error)
    assert title.startswith(UNEXPECTED_ERROR_TITLE_PREFIX)
    assert "ValueError" in title
    assert "Something went wrong" in title
    assert "Second line details" not in title


def test_build_unexpected_error_issue_title_handles_empty_message() -> None:
    """build_unexpected_error_issue_title should handle errors with no message."""
    error = RuntimeError()
    title = build_unexpected_error_issue_title(error)
    assert "RuntimeError" in title
    assert "No message" in title


def test_build_unexpected_error_issue_body_includes_traceback() -> None:
    """build_unexpected_error_issue_body should include the error type and traceback."""
    error = KeyError("missing_key")
    body = build_unexpected_error_issue_body(error, "Traceback (most recent call last):\n  ...")
    assert "KeyError" in body
    assert "missing_key" in body
    assert "Traceback" in body
    assert "Bug Report" in body


# =============================================================================
# Tests for write_diagnose_context_file
# =============================================================================


def test_write_diagnose_context_file_creates_json() -> None:
    path = write_diagnose_context_file(
        traceback_str="Traceback:\n  ValueError: oops",
        mngr_version="0.2.4",
        error_type="ValueError",
        error_message="oops",
    )
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["traceback_str"] == "Traceback:\n  ValueError: oops"
    assert data["mngr_version"] == "0.2.4"
    assert data["error_type"] == "ValueError"
    assert data["error_message"] == "oops"
    # Clean up
    path.unlink(missing_ok=True)


def test_write_diagnose_context_file_deterministic_name() -> None:
    """Same inputs produce the same file path (content-addressed)."""
    path1 = write_diagnose_context_file("tb", "0.2.4", "Err", "msg")
    path2 = write_diagnose_context_file("tb", "0.2.4", "Err", "msg")
    assert path1 == path2
    assert path1.name.startswith("mngr-diagnose-context-")
    assert path1.name.endswith(".json")
    # Clean up
    path1.unlink(missing_ok=True)


def test_write_diagnose_context_file_different_inputs() -> None:
    """Different inputs produce different file paths."""
    path1 = write_diagnose_context_file("tb1", "0.2.4", "Err", "msg1")
    path2 = write_diagnose_context_file("tb2", "0.2.4", "Err", "msg2")
    assert path1 != path2
    # Clean up
    path1.unlink(missing_ok=True)
    path2.unlink(missing_ok=True)


# =============================================================================
# Tests for _offer_diagnose
# =============================================================================


def test_offer_diagnose_skips_when_autonomous(monkeypatch: pytest.MonkeyPatch) -> None:
    """_offer_diagnose produces no output when IS_AUTONOMOUS=1."""
    monkeypatch.setenv("IS_AUTONOMOUS", "1")
    with capture_loguru(level="INFO") as log_output:
        _offer_diagnose(Path("/tmp/fake-ctx.json"), ctx=None)
    assert log_output.getvalue() == ""


def test_offer_diagnose_shows_install_hint_when_no_plugin() -> None:
    """_offer_diagnose shows install + run instructions when plugin is not installed."""
    with capture_loguru(level="INFO") as log_output:
        _offer_diagnose(Path("/tmp/fake-ctx.json"), ctx=None)
    output = log_output.getvalue()
    assert "mngr plugin add imbue-mngr-diagnose" in output
    assert "mngr diagnose --context-file" in output


# =============================================================================
# Tests for _is_diagnose_command
# =============================================================================


def test_is_diagnose_command_returns_false_for_none() -> None:
    assert _is_diagnose_command(None) is False


def test_is_diagnose_command_returns_false_without_flag() -> None:
    """A context from a non-diagnose command has the flag unset."""
    ctx = click.Context(cli)
    assert _is_diagnose_command(ctx) is False


def test_is_diagnose_command_returns_true_when_flag_set_on_shared_meta() -> None:
    """Diagnose sets the flag via ctx.meta; click shares .meta across the tree.

    This mirrors what happens at runtime: diagnose writes to its own ctx.meta
    before invoking create, and the root context's .meta dict reflects that
    write (they are the same dict).
    """
    root_ctx = click.Context(cli)
    # A child ctx's .meta is the same dict as the parent's
    child_ctx = click.Context(cli, parent=root_ctx)
    child_ctx.meta[DIAGNOSE_FLOW_META_KEY] = True

    assert _is_diagnose_command(root_ctx) is True
