from collections.abc import Sequence
from typing import cast

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from imbue.minds.desktop_client.e2e_workspace_runner import WorkspaceCreationFailedError
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


class _FakeContentPage:
    """A candidate WebContentsView page; the waiter's page scan only reads ``url``.

    ``urls`` is consumed one entry per read; the final entry repeats so a steady
    state (or a workspace that appears after N polls) can be expressed as a list.
    """

    def __init__(self, urls: Sequence[str]) -> None:
        self._urls = list(urls)

    @property
    def url(self) -> str:
        return self._urls.pop(0) if len(self._urls) > 1 else self._urls[0]


class _FakeContext:
    def __init__(self, pages: Sequence[object], browser: object | None = None) -> None:
        self.pages = list(pages)
        self.browser = browser


class _FakeBrowser:
    def __init__(self, contexts: Sequence[_FakeContext]) -> None:
        self.contexts = list(contexts)


class _FakeCreatingPage:
    """Duck-typed stand-in for the chrome-view page the create form is driven on.

    After the content-in-chrome split the ready workspace opens on a *separate*
    content-view page, so the waiter scans ``browser.contexts[*].pages`` for the
    one that reached the ``agent-<id>.localhost`` URL, and watches THIS page's
    ``#failure-view`` for the failure branch. ``urls`` / ``is_visible_results``
    are consumed one entry per poll iteration; the final entry repeats. An
    ``is_visible_results`` entry that is an exception is raised, simulating an
    execution-context-destroyed error when the chrome view swaps to ``/_chrome``.
    """

    def __init__(
        self,
        *,
        urls: Sequence[str],
        is_visible_results: Sequence[bool | BaseException] = (),
        candidate_pages: Sequence[_FakeContentPage] = (),
        error_message: str | None = None,
    ) -> None:
        self._urls = list(urls)
        self._is_visible_results = list(is_visible_results)
        self._error_message = error_message
        self.wait_for_timeout_calls = 0
        pages: list[object] = [self, *candidate_pages]
        self._browser = _FakeBrowser([_FakeContext(pages)])
        self.context = _FakeContext(pages, browser=self._browser)

    @property
    def browser(self) -> _FakeBrowser:
        return self._browser

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


def test_wait_returns_the_content_page_that_reached_the_workspace() -> None:
    workspace = _FakeContentPage(urls=[_READY_URL])
    creating = _FakeCreatingPage(urls=[_PENDING_URL], is_visible_results=[False], candidate_pages=[workspace])
    # Returns the content-view page once its agent-subdomain URL is reached (the
    # chrome view that drove the form -- ``creating`` -- stays on /create-ish).
    result = _wait_for_workspace_ready_or_failure(
        cast(Browser, creating.browser), cast(Page, creating), timeout_seconds=5
    )
    assert result is cast(Page, workspace)


def test_wait_returns_for_https_workspace_url() -> None:
    """The workspace origin is https when the proxy serves TLS + HTTP/2 (the default).

    The ready-check must recognize that scheme, not just http -- otherwise the
    waiter never sees the workspace as ready and times out even though it loaded.
    """
    https_ready_url = "https://agent-deadbeef.localhost:8421/"
    workspace = _FakeContentPage(urls=[https_ready_url])
    creating = _FakeCreatingPage(urls=[_PENDING_URL], is_visible_results=[False], candidate_pages=[workspace])
    result = _wait_for_workspace_ready_or_failure(
        cast(Browser, creating.browser), cast(Page, creating), timeout_seconds=5
    )
    assert result is cast(Page, workspace)


def test_wait_raises_with_surfaced_error_on_failure_view() -> None:
    # No candidate page ever reaches the workspace; the creating page's failure
    # view becomes visible, so the waiter raises with the surfaced error text.
    creating = _FakeCreatingPage(
        urls=[_PENDING_URL],
        is_visible_results=[True],
        candidate_pages=[_FakeContentPage(urls=[_PENDING_URL])],
        error_message="unknown or invalid runtime name: runsc",
    )
    with pytest.raises(WorkspaceCreationFailedError) as exc_info:
        _wait_for_workspace_ready_or_failure(cast(Browser, creating.browser), cast(Page, creating), timeout_seconds=5)
    # The surfaced error text rides along so the failure is diagnosable.
    assert "runsc" in str(exc_info.value)


def test_wait_recovers_from_context_destroyed_during_redirect() -> None:
    # The first failure-view check raises (the chrome view swapped to /_chrome and
    # destroyed the execution context); the next poll sees the content page reach
    # the workspace URL and returns it cleanly.
    workspace = _FakeContentPage(urls=[_PENDING_URL, _READY_URL])
    creating = _FakeCreatingPage(
        urls=[_PENDING_URL],
        is_visible_results=[PlaywrightError("Execution context was destroyed")],
        candidate_pages=[workspace],
    )
    result = _wait_for_workspace_ready_or_failure(
        cast(Browser, creating.browser), cast(Page, creating), timeout_seconds=5
    )
    assert result is cast(Page, workspace)
    assert creating.wait_for_timeout_calls == 1


def test_wait_times_out_when_neither_state_reached() -> None:
    creating = _FakeCreatingPage(
        urls=[_PENDING_URL], is_visible_results=[False], candidate_pages=[_FakeContentPage(urls=[_PENDING_URL])]
    )
    with pytest.raises(PlaywrightTimeoutError):
        _wait_for_workspace_ready_or_failure(cast(Browser, creating.browser), cast(Page, creating), timeout_seconds=0)


def test_read_failure_message_returns_trimmed_text() -> None:
    page = _FakeCreatingPage(urls=[_PENDING_URL], is_visible_results=[False], error_message="  boom  ")
    assert _read_failure_message(cast(Page, page)) == "boom"


def test_read_failure_message_handles_missing_element() -> None:
    page = _FakeCreatingPage(urls=[_PENDING_URL], is_visible_results=[False], error_message=None)
    assert "not present" in _read_failure_message(cast(Page, page))
