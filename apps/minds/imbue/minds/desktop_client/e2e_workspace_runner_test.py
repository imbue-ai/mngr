from collections.abc import Sequence
from typing import cast

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from imbue.minds.desktop_client.e2e_workspace_runner import WorkspaceCreationFailedError
from imbue.minds.desktop_client.e2e_workspace_runner import _current_mngr_branch
from imbue.minds.desktop_client.e2e_workspace_runner import _read_failure_message
from imbue.minds.desktop_client.e2e_workspace_runner import _wait_for_workspace_ready_or_failure

# A workspace-ready URL (matches the agent-subdomain pattern) and a still-pending
# backend URL (does not), used to drive the waiter's success/failure branches.
_READY_URL = "http://agent-deadbeef.localhost:8080/"
_PENDING_URL = "http://localhost:8080/create"


class _FakeElement:
    def __init__(self, text: str) -> None:
        self._text = text

    def inner_text(self) -> str:
        return self._text


class _FakePage:
    """Duck-typed stand-in for the handful of Playwright ``Page`` methods the waiter calls.

    ``urls`` and ``is_visible_results`` are consumed one entry per poll
    iteration; the final entry repeats so a steady state can be expressed with
    a single-element list. An ``is_visible_results`` entry that is an exception
    is raised, simulating an execution-context-destroyed error mid-redirect.
    """

    def __init__(
        self,
        *,
        urls: Sequence[str],
        is_visible_results: Sequence[bool | BaseException],
        error_message: str | None = None,
    ) -> None:
        self._urls = list(urls)
        self._is_visible_results = list(is_visible_results)
        self._error_message = error_message
        self.wait_for_timeout_calls = 0

    @property
    def url(self) -> str:
        return self._urls.pop(0) if len(self._urls) > 1 else self._urls[0]

    def is_visible(self, selector: str) -> bool:
        result = self._is_visible_results.pop(0) if len(self._is_visible_results) > 1 else self._is_visible_results[0]
        if isinstance(result, BaseException):
            raise result
        return result

    def query_selector(self, selector: str) -> _FakeElement | None:
        if selector == "#error-message" and self._error_message is not None:
            return _FakeElement(self._error_message)
        return None

    def wait_for_timeout(self, timeout_ms: float) -> None:
        self.wait_for_timeout_calls += 1


def test_wait_returns_when_workspace_url_reached() -> None:
    page = _FakePage(urls=[_READY_URL], is_visible_results=[False])
    # Returns without raising once the agent-subdomain URL is reached.
    _wait_for_workspace_ready_or_failure(cast(Page, page), timeout_seconds=5)


def test_wait_raises_with_surfaced_error_on_failure_view() -> None:
    page = _FakePage(
        urls=[_PENDING_URL],
        is_visible_results=[True],
        error_message="unknown or invalid runtime name: runsc",
    )
    with pytest.raises(WorkspaceCreationFailedError) as exc_info:
        _wait_for_workspace_ready_or_failure(cast(Page, page), timeout_seconds=5)
    # The surfaced error text rides along so the failure is diagnosable.
    assert "runsc" in str(exc_info.value)


def test_wait_recovers_from_context_destroyed_during_redirect() -> None:
    # The first failure-view check raises (a redirect destroyed the execution
    # context); the next poll sees the workspace URL and returns cleanly.
    page = _FakePage(
        urls=[_PENDING_URL, _READY_URL],
        is_visible_results=[PlaywrightError("Execution context was destroyed")],
    )
    _wait_for_workspace_ready_or_failure(cast(Page, page), timeout_seconds=5)
    assert page.wait_for_timeout_calls == 1


def test_wait_times_out_when_neither_state_reached() -> None:
    page = _FakePage(urls=[_PENDING_URL], is_visible_results=[False])
    with pytest.raises(PlaywrightTimeoutError):
        _wait_for_workspace_ready_or_failure(cast(Page, page), timeout_seconds=0)


def test_read_failure_message_returns_trimmed_text() -> None:
    page = _FakePage(urls=[_PENDING_URL], is_visible_results=[False], error_message="  boom  ")
    assert _read_failure_message(cast(Page, page)) == "boom"


def test_read_failure_message_handles_missing_element() -> None:
    page = _FakePage(urls=[_PENDING_URL], is_visible_results=[False], error_message=None)
    assert "not present" in _read_failure_message(cast(Page, page))


def test_current_mngr_branch_prefers_github_head_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a pull_request CI run the checkout is detached, so the PR source
    branch must come from GITHUB_HEAD_REF rather than `git rev-parse HEAD`."""
    monkeypatch.setenv("GITHUB_HEAD_REF", "mngr/some-feature")
    monkeypatch.setenv("GITHUB_REF_NAME", "123/merge")
    assert _current_mngr_branch() == "mngr/some-feature"


def test_current_mngr_branch_uses_github_ref_name_for_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a push CI run GITHUB_HEAD_REF is unset and GITHUB_REF_NAME is the branch."""
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    assert _current_mngr_branch() == "main"


def test_current_mngr_branch_ignores_pr_merge_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR's GITHUB_REF_NAME is a `<n>/merge` ref, not a real branch; it must be
    ignored so resolution falls through to git rather than asking FCT for a
    `<n>/merge` branch."""
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)
    monkeypatch.setenv("GITHUB_REF_NAME", "2065/merge")
    assert _current_mngr_branch() != "2065/merge"
