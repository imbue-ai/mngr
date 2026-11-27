import io
import json
import re
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from loguru import logger as loguru_logger
from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from pydantic import SecretStr

from imbue.minds.desktop_client.e2e_workspace_runner import DESKTOP_BACKDROP_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import E2E_HARNESS_MARKER_ENV_VAR
from imbue.minds.desktop_client.e2e_workspace_runner import HomeRowState
from imbue.minds.desktop_client.e2e_workspace_runner import LeaveFirstRunProviderPageSignedOut
from imbue.minds.desktop_client.e2e_workspace_runner import MAIN_CHAT_ROW_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import SignInOnFirstRunProviderPage
from imbue.minds.desktop_client.e2e_workspace_runner import TERMINAL_IFRAME_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import WorkspaceCreateAttemptFailedError
from imbue.minds.desktop_client.e2e_workspace_runner import WorkspaceFlowError
from imbue.minds.desktop_client.e2e_workspace_runner import _BACKEND_READY_OUTPUT_MARKER
from imbue.minds.desktop_client.e2e_workspace_runner import _ELECTRON_MAIN_JS
from imbue.minds.desktop_client.e2e_workspace_runner import _ELECTRON_OUTPUT_MAX_LINE_LENGTH
from imbue.minds.desktop_client.e2e_workspace_runner import _ELECTRON_RECENT_OUTPUT_MAX_CHARACTER_COUNT
from imbue.minds.desktop_client.e2e_workspace_runner import _ELECTRON_RECENT_OUTPUT_MAX_LINE_COUNT
from imbue.minds.desktop_client.e2e_workspace_runner import _ELECTRON_STARTUP_OUTPUT_MAX_LINE_COUNT
from imbue.minds.desktop_client.e2e_workspace_runner import _ElectronConnectError
from imbue.minds.desktop_client.e2e_workspace_runner import _ElectronOutputTail
from imbue.minds.desktop_client.e2e_workspace_runner import _LAUNCHER_INPUT_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import _LAUNCHER_OVERLAY_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import _LatchkeyForwardStart
from imbue.minds.desktop_client.e2e_workspace_runner import _START_WORKSPACE_CONTROL_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import _STOP_WORKSPACE_CONTROL_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import _build_electron_env
from imbue.minds.desktop_client.e2e_workspace_runner import _clean_electron_output_line
from imbue.minds.desktop_client.e2e_workspace_runner import _describe_spent_launch_attempts
from imbue.minds.desktop_client.e2e_workspace_runner import _drain_byte_stream_to_loguru
from imbue.minds.desktop_client.e2e_workspace_runner import _read_failure_message
from imbue.minds.desktop_client.e2e_workspace_runner import chat_frame
from imbue.minds.desktop_client.e2e_workspace_runner import complete_first_run
from imbue.minds.desktop_client.e2e_workspace_runner import enter_workspace_from_home_page
from imbue.minds.desktop_client.e2e_workspace_runner import find_chat_frame
from imbue.minds.desktop_client.e2e_workspace_runner import first_run_providers_frame
from imbue.minds.desktop_client.e2e_workspace_runner import is_home_row_running
from imbue.minds.desktop_client.e2e_workspace_runner import is_home_row_stopped
from imbue.minds.desktop_client.e2e_workspace_runner import model_of_assistant_reply_containing
from imbue.minds.desktop_client.e2e_workspace_runner import open_account_home_page
from imbue.minds.desktop_client.e2e_workspace_runner import press_first_run_next
from imbue.minds.desktop_client.e2e_workspace_runner import read_model_of_chat_reply
from imbue.minds.desktop_client.e2e_workspace_runner import send_to_main_chat_from_launcher
from imbue.minds.desktop_client.e2e_workspace_runner import start_workspace_from_home_page
from imbue.minds.desktop_client.e2e_workspace_runner import stop_workspace_from_home_page
from imbue.minds.desktop_client.e2e_workspace_runner import switch_chat_to_cheapest_model
from imbue.minds.desktop_client.e2e_workspace_runner import wait_for_workspace_ready_or_failure
from imbue.minds.desktop_client.e2e_workspace_runner import wait_out_first_run_cover

# A workspace-ready URL (matches the agent-subdomain pattern) and a still-pending
# backend URL (does not), used to drive the waiter's success/failure branches.
_READY_URL = "http://host-0123456789abcdef0123456789abcdef.localhost:8080/"
_PENDING_URL = "http://localhost:8080/create"


class _FakeElement:
    def __init__(self, text: str, on_click: Callable[[], None] = lambda: None) -> None:
        self._text = text
        self._on_click = on_click

    def inner_text(self) -> str:
        return self._text

    def click(self) -> None:
        self._on_click()


class _FakeFrame:
    """A frame of a candidate page: the waiter's frame scan reads ``url``, and the chat-frame scan also walks
    ``child_frames``.

    ``urls`` is consumed one entry per read; the final entry repeats so a steady
    state (or a machine that appears after N polls) can be expressed as a list.
    """

    def __init__(self, urls: Sequence[str], child_frames: Sequence["_FakeFrame"] = ()) -> None:
        self._urls = list(urls)
        self.child_frames = list(child_frames)

    @property
    def url(self) -> str:
        return self._urls.pop(0) if len(self._urls) > 1 else self._urls[0]


class _FakeContentPage:
    """A candidate page hosting frames (the workspace surface is an iframe now)."""

    def __init__(self, urls: Sequence[str]) -> None:
        self.main_frame = _FakeFrame(urls)

    @property
    def frames(self) -> "list[_FakeFrame]":
        return [self.main_frame]

    @property
    def url(self) -> str:
        return self.main_frame.url


class _FakeContext:
    def __init__(self, pages: Sequence[object], browser: object | None = None) -> None:
        self.pages = list(pages)
        self.browser = browser


class _FakeBrowser:
    def __init__(self, contexts: Sequence[_FakeContext]) -> None:
        self.contexts = list(contexts)


class _FakeCreatingPage:
    """Duck-typed stand-in for the chrome-view page the create form is driven on.

    The ready workspace opens inside the chrome page's content IFRAME, so the
    waiter scans every page's frames for the one that reached the
    ``host-<id>.localhost`` URL, and watches THIS page's ``#failure-view`` for
    the failure branch. ``urls`` / ``is_visible_results``
    are consumed one entry per poll iteration; the final entry repeats. An
    ``is_visible_results`` entry that is an exception is raised, simulating an
    execution-context-destroyed error when the page routes onto ``/workspace/<id>``.

    The selectors it answers are the ids a failed create puts on screen
    (``CreateFailure.ts``): ``#error-message`` around the sentence, the
    ``#failure-view-details`` toggle and the ``#failure-view-details-panel`` it
    opens. Anything else returns nothing, as it would on the real page.
    """

    def __init__(
        self,
        *,
        urls: Sequence[str],
        is_visible_results: Sequence[bool | BaseException] = (),
        candidate_pages: Sequence[_FakeContentPage] = (),
        error_message: str | None = None,
        error_details: str | None = None,
    ) -> None:
        self._urls = list(urls)
        self._is_visible_results = list(is_visible_results)
        self._error_message = error_message
        self._error_details = error_details
        self._is_details_open = False
        self._is_details_redraw_pending = False
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

    @property
    def frames(self) -> list[object]:
        return []

    def is_visible(self, selector: str) -> bool:
        result = self._is_visible_results.pop(0) if len(self._is_visible_results) > 1 else self._is_visible_results[0]
        if isinstance(result, BaseException):
            raise result
        return result

    def query_selector(self, selector: str) -> _FakeElement | None:
        if selector == "#error-message" and self._error_message is not None:
            return _FakeElement(self._error_message)
        if selector == "#failure-view-details" and self._error_details is not None:
            return _FakeElement("See details", on_click=self._open_details)
        if selector == "#failure-view-details-panel" and self._is_details_open:
            return _FakeElement(self._error_details or "")
        return None

    def _open_details(self) -> None:
        # The page redraws after the click handler returns, so the panel is not drawn yet.
        self._is_details_redraw_pending = True

    def wait_for_selector(self, selector: str, *, state: str, timeout: float) -> _FakeElement:
        if selector == "#failure-view-details-panel" and self._is_details_redraw_pending:
            self._is_details_open = True
        element = self.query_selector(selector)
        if element is None:
            raise PlaywrightTimeoutError(f"{selector} not {state} within {timeout}ms")
        return element

    def wait_for_timeout(self, timeout_ms: float) -> None:
        self.wait_for_timeout_calls += 1


def test_wait_returns_the_content_page_that_reached_the_workspace() -> None:
    workspace = _FakeContentPage(urls=[_READY_URL])
    creating = _FakeCreatingPage(urls=[_PENDING_URL], is_visible_results=[False], candidate_pages=[workspace])
    # Returns the workspace frame once its agent-subdomain URL is reached (the
    # chrome page that drove the form -- ``creating`` -- stays on /create-ish).
    result = wait_for_workspace_ready_or_failure(
        cast(Browser, creating.browser), cast(Page, creating), timeout_seconds=5
    )
    assert result is cast(Frame, workspace.main_frame)


def test_wait_returns_for_https_workspace_url() -> None:
    """The machine origin is https when the proxy serves TLS + HTTP/2 (the default).

    The ready-check must recognize that scheme, not just http -- otherwise the
    waiter never sees the machine as ready and times out even though it loaded.
    """
    https_ready_url = "https://host-0123456789abcdef0123456789abcdef.localhost:8421/"
    workspace = _FakeContentPage(urls=[https_ready_url])
    creating = _FakeCreatingPage(urls=[_PENDING_URL], is_visible_results=[False], candidate_pages=[workspace])
    result = wait_for_workspace_ready_or_failure(
        cast(Browser, creating.browser), cast(Page, creating), timeout_seconds=5
    )
    assert result is cast(Frame, workspace.main_frame)


def test_wait_raises_with_surfaced_error_on_failure_view() -> None:
    # No candidate page ever reaches the workspace; the creating page's failure
    # view becomes visible, so the waiter raises with the surfaced error text.
    creating = _FakeCreatingPage(
        urls=[_PENDING_URL],
        is_visible_results=[True],
        candidate_pages=[_FakeContentPage(urls=[_PENDING_URL])],
        error_message="Couldn't create alpha. Try again. If this persists, report a bug.",
        error_details="mngr create failed (exit code 1):\nunknown or invalid runtime name: runsc",
    )
    with pytest.raises(WorkspaceCreateAttemptFailedError) as exc_info:
        wait_for_workspace_ready_or_failure(cast(Browser, creating.browser), cast(Page, creating), timeout_seconds=5)
    # The page's sentence names no cause; the raw error behind See details rides along so the failure is diagnosable.
    assert "Couldn't create alpha." in str(exc_info.value)
    assert "runsc" in str(exc_info.value)


def test_wait_recovers_from_context_destroyed_during_redirect() -> None:
    # The first failure-view check raises (the page routed onto /workspace/<id>
    # and destroyed the execution context); the next poll sees the content page
    # reach the workspace URL and returns it cleanly.
    workspace = _FakeContentPage(urls=[_PENDING_URL, _READY_URL])
    creating = _FakeCreatingPage(
        urls=[_PENDING_URL],
        is_visible_results=[PlaywrightError("Execution context was destroyed")],
        candidate_pages=[workspace],
    )
    result = wait_for_workspace_ready_or_failure(
        cast(Browser, creating.browser), cast(Page, creating), timeout_seconds=5
    )
    assert result is cast(Frame, workspace.main_frame)
    assert creating.wait_for_timeout_calls == 1


def test_wait_times_out_when_neither_state_reached() -> None:
    creating = _FakeCreatingPage(
        urls=[_PENDING_URL], is_visible_results=[False], candidate_pages=[_FakeContentPage(urls=[_PENDING_URL])]
    )
    with pytest.raises(PlaywrightTimeoutError):
        wait_for_workspace_ready_or_failure(cast(Browser, creating.browser), cast(Page, creating), timeout_seconds=0)


def test_read_failure_message_returns_trimmed_text() -> None:
    page = _FakeCreatingPage(urls=[_PENDING_URL], is_visible_results=[False], error_message="  boom  ")
    assert _read_failure_message(cast(Page, page)) == "boom"


def test_read_failure_message_handles_missing_element() -> None:
    page = _FakeCreatingPage(urls=[_PENDING_URL], is_visible_results=[False], error_message=None)
    assert "not present" in _read_failure_message(cast(Page, page))


def _terminal_selector_prefixes() -> list[str]:
    """The ``src^="..."`` prefix literals the terminal-iframe selector keys on."""
    return re.findall(r'src\^="([^"]+)"', TERMINAL_IFRAME_SELECTOR)


def test_terminal_iframe_selector_matches_the_labelled_origin() -> None:
    # The terminal's origin label is ``terminal-<rand>``, so its iframe src is
    # ``https://terminal-<rand>.host-<hex>.localhost:<port>/``. The selector must
    # match that (a ``src^=`` is a startswith), and precisely: it must NOT match a
    # bare ``terminal.`` origin (the old label==name assumption) nor an unrelated
    # ``terminals`` service whose name merely starts with "terminal".
    prefixes = _terminal_selector_prefixes()
    labelled = "https://terminal-x7k9q2w1.host-0123456789abcdef.localhost:8421/"
    bare = "https://terminal.host-0123456789abcdef.localhost:8421/"
    unrelated = "https://terminals.host-0123456789abcdef.localhost:8421/"
    assert any(labelled.startswith(prefix) for prefix in prefixes)
    assert not any(bare.startswith(prefix) for prefix in prefixes)
    assert not any(unrelated.startswith(prefix) for prefix in prefixes)


# The workspace shell's own origin, a chat page framed at the chat app's origin (its path is the
# chat's agent id), and a terminal iframe (path ``/``) -- the frames a workspace has open.
_WORKSPACE_AGENT_ID = "agent-0123456789abcdef0123456789abcdef"
_WORKSPACE_SHELL_URL = f"https://{_WORKSPACE_AGENT_ID}.localhost:8421/"
_CHAT_AGENT_ID = "agent-fedcba9876543210fedcba9876543210"
_CHAT_PAGE_URL = f"https://chat-x7k9q2w1.{_WORKSPACE_AGENT_ID}.localhost:8421/{_CHAT_AGENT_ID}"
# The welcome chat the creation page seeded, which a fresh workspace opens on before any row is pressed.
_WELCOME_CHAT_ID = "agent-0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"
_WELCOME_CHAT_PAGE_URL = f"https://chat-x7k9q2w1.{_WORKSPACE_AGENT_ID}.localhost:8421/{_WELCOME_CHAT_ID}"
_TERMINAL_PAGE_URL = f"https://terminal-x7k9q2w1.{_WORKSPACE_AGENT_ID}.localhost:8421/"


class _FakeWorkspaceFrame:
    """The workspace frame: ``chat_frame`` scans the frames under it and polls with ``wait_for_timeout``.

    ``child_frame_lists`` is consumed one entry per scan; the final entry repeats, so a chat frame
    that attaches after N polls is a list whose later entries include it.
    """

    def __init__(self, child_frame_lists: Sequence[Sequence[_FakeFrame]]) -> None:
        self._child_frame_lists = [list(frames) for frames in child_frame_lists]
        self.wait_for_timeout_calls = 0

    @property
    def child_frames(self) -> list[_FakeFrame]:
        return self._child_frame_lists.pop(0) if len(self._child_frame_lists) > 1 else self._child_frame_lists[0]

    def wait_for_timeout(self, timeout_ms: float) -> None:
        self.wait_for_timeout_calls += 1


def test_chat_frame_is_the_child_whose_path_is_the_chat_agent_id() -> None:
    terminal = _FakeFrame(urls=[_TERMINAL_PAGE_URL])
    chat = _FakeFrame(urls=[_CHAT_PAGE_URL])
    workspace = _FakeWorkspaceFrame(child_frame_lists=[[terminal, chat]])
    assert chat_frame(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, chat)
    assert workspace.wait_for_timeout_calls == 0


def test_chat_frame_is_found_under_the_chat_root_the_desktop_frames() -> None:
    # The desktop frames the chat app's root (path ``/``), and the root frames the chat: the chat
    # page is the workspace's grandchild, and the root itself is not a chat page.
    chat = _FakeFrame(urls=[_CHAT_PAGE_URL])
    root = _FakeFrame(urls=[f"https://chat-x7k9q2w1.{_WORKSPACE_AGENT_ID}.localhost:8421/"], child_frames=[chat])
    workspace = _FakeWorkspaceFrame(child_frame_lists=[[root]])
    assert chat_frame(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, chat)


def test_find_chat_frame_is_the_page_of_the_chat_it_names_and_none_while_no_page_shows_it() -> None:
    welcome = _FakeFrame(urls=[_WELCOME_CHAT_PAGE_URL])
    chat = _FakeFrame(urls=[_CHAT_PAGE_URL])
    root = _FakeFrame(
        urls=[f"https://chat-x7k9q2w1.{_WORKSPACE_AGENT_ID}.localhost:8421/"], child_frames=[welcome, chat]
    )
    workspace = _FakeWorkspaceFrame(child_frame_lists=[[root]])
    assert find_chat_frame(cast(Frame, workspace), _CHAT_AGENT_ID) is cast(Frame, chat)
    assert find_chat_frame(cast(Frame, workspace), _WORKSPACE_AGENT_ID) is None


def test_chat_frame_polls_until_the_chat_attaches() -> None:
    # The shell frames the chat's page after the row is pressed, so it is not
    # there on the first scan; the poll runs through Playwright's own wait.
    chat = _FakeFrame(urls=[_CHAT_PAGE_URL])
    workspace = _FakeWorkspaceFrame(child_frame_lists=[[], [chat]])
    assert chat_frame(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, chat)
    assert workspace.wait_for_timeout_calls == 1


def test_chat_frame_ignores_the_agent_id_in_a_host_name() -> None:
    # Only a URL PATH ending in the agent id is a chat page: the workspace shell
    # and its service iframes carry the agent id in their host names, on path ``/``.
    workspace = _FakeWorkspaceFrame(child_frame_lists=[[_FakeFrame(urls=[_WORKSPACE_SHELL_URL])]])
    with pytest.raises(WorkspaceFlowError):
        chat_frame(cast(Frame, workspace), timeout_seconds=0)


def test_chat_frame_raises_when_no_chat_opens_in_time() -> None:
    workspace = _FakeWorkspaceFrame(child_frame_lists=[[_FakeFrame(urls=[_TERMINAL_PAGE_URL])]])
    with pytest.raises(WorkspaceFlowError):
        chat_frame(cast(Frame, workspace), timeout_seconds=0)


_FIRST_RUN_PROVIDERS_URL = f"https://chat-x7k9q2w1.{_WORKSPACE_AGENT_ID}.localhost:8421/?view=first-run"
_STUDIO_COVER = "#first-run-cover"
_WORKSPACE_COVER = "iframe#first-run-cover"
_PROVIDERS_PAGE = "#first-run-providers"
_ANTHROPIC_TILE = '[data-first-run-lane="anthropic"]'
_API_KEY_METHOD = "[data-e2e=method-api_key]"
_ANOTHER_WAY = "[data-e2e=sign-in-another-way]"


class _FakeCoveredChromePage:
    """The chrome page while the app's cover is over a new workspace: records every wait and click, in order."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, ...]] = []

    def wait_for_selector(self, selector: str, *, state: str, timeout: float) -> None:
        self.actions.append(("wait", selector, state))

    def click(self, selector: str) -> None:
        self.actions.append(("click", selector))


class _FakeProvidersFrame:
    """The frame of the template's cover over a new workspace's desktop.

    ``drawn_reads`` says, one entry per read, whether the provider page is drawn in it (the final entry
    repeats); ``account_ids`` are the accounts the chat app lists to it. The chooser a tile opens offers its
    API-key method at once unless ``is_browser_sign_in_relayed``, where it is behind "Try another way". Every
    click and fill is recorded, in order.
    """

    def __init__(
        self,
        *,
        drawn_reads: Sequence[bool] = (True,),
        account_ids: Sequence[str] = (),
        is_browser_sign_in_relayed: bool = False,
    ) -> None:
        self.url = _FIRST_RUN_PROVIDERS_URL
        self._drawn_reads = list(drawn_reads)
        self._account_ids = list(account_ids)
        self._is_api_key_method_offered = not is_browser_sign_in_relayed
        self.actions: list[tuple[str, ...]] = []

    def query_selector(self, selector: str) -> _FakeElement | None:
        if selector == _PROVIDERS_PAGE:
            is_drawn = self._drawn_reads.pop(0) if len(self._drawn_reads) > 1 else self._drawn_reads[0]
            return _FakeElement("Your own cloud computer, any AI") if is_drawn else None
        if selector == _API_KEY_METHOD and self._is_api_key_method_offered:
            return _FakeElement("API key")
        return None

    def evaluate(self, expression: str, arg: Mapping[str, object] | None = None) -> object:
        if arg is None:
            return "Connect an AI provider | That didn't go through. Try again."
        assert (arg["method"], arg["path"]) == ("GET", "/api/accounts")
        accounts = [{"id": account_id} for account_id in self._account_ids]
        return {"status": 200, "body": json.dumps({"accounts": accounts})}

    def click(self, selector: str, timeout: float | None = None) -> None:
        self.actions.append(("click", selector))
        if selector == _ANOTHER_WAY:
            self._is_api_key_method_offered = True

    def fill(self, selector: str, value: str) -> None:
        self.actions.append(("fill", selector, value))

    def wait_for_selector(self, selector: str, state: str = "visible", timeout: float | None = None) -> None:
        pass

    def is_detached(self) -> bool:
        return False


class _FakeCoverElement:
    """The ``iframe`` element of the template's cover, which answers with the frame it holds."""

    def __init__(self, frame: _FakeProvidersFrame) -> None:
        self._frame = frame

    def content_frame(self) -> _FakeProvidersFrame:
        return self._frame


class _FakeFirstRunWorkspace:
    """The workspace frame of a just-created workspace.

    ``cover_reads`` says, one entry per read, the frame its cover holds, or None while it has no cover up (the
    final entry repeats). What it is asked to wait for is recorded, in order, and ``is_cover_kept_up`` makes
    the wait for the cover to go time out.
    """

    def __init__(self, cover_reads: Sequence[_FakeProvidersFrame | None], *, is_cover_kept_up: bool = False) -> None:
        self.url = _WORKSPACE_SHELL_URL
        self._cover_reads = list(cover_reads)
        self._is_cover_kept_up = is_cover_kept_up
        self.waits: list[tuple[str, str]] = []
        self.wait_for_timeout_calls = 0

    def query_selector(self, selector: str) -> _FakeCoverElement | None:
        assert selector == _WORKSPACE_COVER
        cover_frame = self._cover_reads.pop(0) if len(self._cover_reads) > 1 else self._cover_reads[0]
        return None if cover_frame is None else _FakeCoverElement(cover_frame)

    def wait_for_selector(self, selector: str, *, state: str, timeout: float) -> None:
        if selector == _WORKSPACE_COVER and self._is_cover_kept_up:
            raise PlaywrightTimeoutError(f"{selector} not {state} within {timeout}ms")
        self.waits.append((selector, state))

    def wait_for_timeout(self, timeout_ms: float) -> None:
        self.wait_for_timeout_calls += 1


def test_waiting_out_the_cover_presses_nothing_and_waits_for_the_cover_to_go() -> None:
    page = _FakeCoveredChromePage()
    wait_out_first_run_cover(cast(Page, page))
    assert page.actions == [("wait", _STUDIO_COVER, "detached")]


def test_the_provider_page_s_frame_is_the_one_the_workspace_s_cover_holds() -> None:
    providers = _FakeProvidersFrame()
    workspace = _FakeFirstRunWorkspace(cover_reads=[providers])
    assert first_run_providers_frame(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, providers)
    assert workspace.wait_for_timeout_calls == 0


def test_the_provider_page_s_frame_is_waited_for_until_the_cover_is_up_and_draws_the_page() -> None:
    providers = _FakeProvidersFrame(drawn_reads=[False, True])
    workspace = _FakeFirstRunWorkspace(cover_reads=[None, providers])
    assert first_run_providers_frame(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, providers)
    assert workspace.wait_for_timeout_calls == 2


def test_a_workspace_that_never_puts_its_cover_up_raises_saying_so() -> None:
    workspace = _FakeFirstRunWorkspace(cover_reads=[None])
    with pytest.raises(WorkspaceFlowError, match="no cover over its desktop"):
        first_run_providers_frame(cast(Frame, workspace), timeout_seconds=0)


def test_a_cover_that_never_draws_the_provider_page_raises_with_what_its_frame_shows() -> None:
    workspace = _FakeFirstRunWorkspace(cover_reads=[_FakeProvidersFrame(drawn_reads=[False])])
    with pytest.raises(WorkspaceFlowError, match="its cover up without the page") as raised:
        first_run_providers_frame(cast(Frame, workspace), timeout_seconds=0)
    assert _FIRST_RUN_PROVIDERS_URL in str(raised.value)


def test_the_first_run_is_left_with_skip_on_a_workspace_that_holds_no_account() -> None:
    page = _FakeCoveredChromePage()
    providers = _FakeProvidersFrame(account_ids=[])
    workspace = _FakeFirstRunWorkspace(cover_reads=[providers])

    complete_first_run(cast(Page, page), cast(Frame, workspace), LeaveFirstRunProviderPageSignedOut(), label="test")

    assert page.actions[0] == ("wait", _STUDIO_COVER, "detached")
    assert providers.actions == [("click", "#first-run-skip")]
    assert workspace.waits == [(_WORKSPACE_COVER, "detached"), (DESKTOP_BACKDROP_SELECTOR, "visible")]


def test_the_first_run_is_left_with_next_on_a_workspace_that_already_holds_an_account() -> None:
    # The page offers Skip only while nothing is connected.
    providers = _FakeProvidersFrame(account_ids=["account-synced-64113"])
    workspace = _FakeFirstRunWorkspace(cover_reads=[providers])

    complete_first_run(
        cast(Page, _FakeCoveredChromePage()),
        cast(Frame, workspace),
        LeaveFirstRunProviderPageSignedOut(),
        label="test",
    )

    assert providers.actions == [("click", "#first-run-next")]


def test_signing_in_on_the_first_run_goes_from_the_anthropic_tile_through_the_api_key_method_to_next() -> None:
    providers = _FakeProvidersFrame()
    workspace = _FakeFirstRunWorkspace(cover_reads=[providers])

    complete_first_run(
        cast(Page, _FakeCoveredChromePage()),
        cast(Frame, workspace),
        SignInOnFirstRunProviderPage(api_key=SecretStr("sk-ant-test-80417")),
        label="test",
    )

    assert providers.actions == [
        ("click", _ANTHROPIC_TILE),
        ("click", _API_KEY_METHOD),
        ("fill", "[data-e2e=api-key-input]", "sk-ant-test-80417"),
        ("click", "[data-e2e=save-key]"),
        ("click", "#first-run-next"),
    ]
    assert workspace.waits == [(_WORKSPACE_COVER, "detached"), (DESKTOP_BACKDROP_SELECTOR, "visible")]


def test_signing_in_on_the_first_run_takes_another_way_when_the_chooser_waits_on_a_relayed_browser_sign_in() -> None:
    providers = _FakeProvidersFrame(is_browser_sign_in_relayed=True)
    workspace = _FakeFirstRunWorkspace(cover_reads=[providers])

    complete_first_run(
        cast(Page, _FakeCoveredChromePage()),
        cast(Frame, workspace),
        SignInOnFirstRunProviderPage(api_key=SecretStr("sk-ant-test-22906")),
        label="test",
    )

    assert providers.actions[:3] == [("click", _ANTHROPIC_TILE), ("click", _ANOTHER_WAY), ("click", _API_KEY_METHOD)]


def test_a_provider_page_that_stays_up_after_next_is_pressed_raises_with_what_the_page_shows() -> None:
    providers = _FakeProvidersFrame(account_ids=["account-kept-39050"])
    workspace = _FakeFirstRunWorkspace(cover_reads=[providers], is_cover_kept_up=True)

    with pytest.raises(WorkspaceFlowError, match="provider page stayed up") as raised:
        press_first_run_next(cast(Frame, workspace), cast(Frame, providers))

    assert "#first-run-next" in str(raised.value)
    assert "That didn't go through" in str(raised.value)
    assert workspace.waits == []


class _FakeTypingLauncherWorkspace(_FakeWorkspaceFrame):
    """The launcher fake for a text typed into the field: records every fill, wait and key press, in order."""

    def __init__(self) -> None:
        super().__init__(child_frame_lists=[])
        self.actions: list[tuple[str, ...]] = []

    def fill(self, selector: str, value: str) -> None:
        self.actions.append(("fill", selector, value))

    def wait_for_selector(self, selector: str, state: str, timeout: float) -> None:
        self.actions.append(("wait", selector))

    def press(self, selector: str, key: str) -> None:
        self.actions.append(("press", selector, key))


def test_send_to_main_chat_types_the_text_and_presses_enter_once_the_main_chat_row_is_the_highlighted_one() -> None:
    workspace = _FakeTypingLauncherWorkspace()
    send_to_main_chat_from_launcher(cast(Frame, workspace), "build me a thing")
    assert workspace.actions == [
        ("fill", _LAUNCHER_INPUT_SELECTOR, "build me a thing"),
        ("wait", f'{_LAUNCHER_OVERLAY_SELECTOR}:visible {MAIN_CHAT_ROW_SELECTOR}[data-highlighted="true"]'),
        ("press", _LAUNCHER_INPUT_SELECTOR, "Enter"),
    ]


def test_build_electron_env_sets_the_harness_marker(tmp_path: Path) -> None:
    env = _build_electron_env(tmp_path)
    assert env[E2E_HARNESS_MARKER_ENV_VAR] == "1"


def test_drain_promotes_gpu_and_signal_lines_to_info() -> None:
    stream = io.BytesIO(
        b"ordinary chatter\n"
        b"[gpu] GPU process gone (reason=killed)\n"
        b"[lifecycle] SIGTERM received, requesting quit (uptime=1.0s)\n"
        b"/path/to/electron exited with signal SIGTRAP\n"
        b"\n"
    )
    records: list[tuple[str, str]] = []
    sink_id = loguru_logger.add(
        lambda msg: records.append((msg.record["level"].name, msg.record["message"])), level="DEBUG"
    )
    output_tail = _ElectronOutputTail()
    try:
        _drain_byte_stream_to_loguru(stream, "electron-err", output_tail)
    finally:
        loguru_logger.remove(sink_id)
    assert records == [
        ("DEBUG", "[electron-err] ordinary chatter"),
        ("INFO", "[electron-err] [gpu] GPU process gone (reason=killed)"),
        ("INFO", "[electron-err] [lifecycle] SIGTERM received, requesting quit (uptime=1.0s)"),
        ("INFO", "[electron-err] /path/to/electron exited with signal SIGTRAP"),
    ]
    assert output_tail.read().recent_lines == tuple(message for _level, message in records)


_ONE_TIME_CODE = "Zq3vK8-w_Yt51secret"
_ATTACH_FAILURE_CAUSE = (
    "Electron CDP attach failed on port 48653: No Electron content page settled on a backend URL within 19s; "
    "observed pages: ['file:///code/mngr/apps/minds/electron/shell.html#intro']"
)


def test_the_backend_ready_marker_is_a_line_main_js_prints() -> None:
    assert f"console.log('{_BACKEND_READY_OUTPUT_MARKER}'" in _ELECTRON_MAIN_JS.read_text()


def test_the_output_tail_keeps_the_newest_lines_within_its_line_count() -> None:
    output_tail = _ElectronOutputTail()
    for idx in range(200):
        output_tail.record("electron-err", f"line {idx}")

    reading = output_tail.read()

    newest_idxs = range(200 - _ELECTRON_RECENT_OUTPUT_MAX_LINE_COUNT, 200)
    assert reading.recent_lines == tuple(f"[electron-err] line {idx}" for idx in newest_idxs)
    assert reading.written_line_count == 200


def test_the_output_tail_keeps_the_newest_lines_within_its_character_count_and_cuts_a_long_line() -> None:
    output_tail = _ElectronOutputTail()
    for idx in range(40):
        output_tail.record("electron-err", f"{idx:03d} " + "x" * 500)

    reading = output_tail.read()

    # Fewer lines than the line count allows, so the character count is what bounds them.
    kept_line_count = _ELECTRON_RECENT_OUTPUT_MAX_CHARACTER_COUNT // _ELECTRON_OUTPUT_MAX_LINE_LENGTH
    assert kept_line_count < _ELECTRON_RECENT_OUTPUT_MAX_LINE_COUNT
    assert [line[:19] for line in reading.recent_lines] == [
        f"[electron-err] {idx:03d} " for idx in range(40 - kept_line_count, 40)
    ]
    assert {len(line) for line in reading.recent_lines} == {_ELECTRON_OUTPUT_MAX_LINE_LENGTH}


def test_the_output_tail_keeps_main_js_s_startup_lines_once_later_output_has_pushed_them_out_of_the_recent_lines() -> (
    None
):
    output_tail = _ElectronOutputTail()
    output_tail.record("electron-out", "[startup] Loading shell.html...")
    output_tail.record("electron-err", "[startup] shell.html load did not finish: ERR_ABORTED")
    for idx in range(500):
        output_tail.record("electron-err", f"backend chatter {idx}")

    reading = output_tail.read()

    assert reading.startup_lines == (
        "[electron-out] [startup] Loading shell.html...",
        "[electron-err] [startup] shell.html load did not finish: ERR_ABORTED",
    )
    assert not any("[startup]" in line for line in reading.recent_lines)
    assert reading.is_backend_ready_seen is False


def test_the_output_tail_remembers_the_backend_ready_line_after_it_has_left_every_kept_line() -> None:
    output_tail = _ElectronOutputTail()
    output_tail.record("electron-out", "[startup] Backend ready at http://localhost:41221")
    for idx in range(100):
        output_tail.record("electron-out", f"[startup] later step {idx}")

    reading = output_tail.read()

    newest_idxs = range(100 - _ELECTRON_STARTUP_OUTPUT_MAX_LINE_COUNT, 100)
    assert reading.startup_lines == tuple(f"[electron-out] [startup] later step {idx}" for idx in newest_idxs)
    assert not any("Backend ready" in line for line in reading.recent_lines)
    assert reading.is_backend_ready_seen is True


@pytest.mark.parametrize(
    ("line", "expected_line"),
    [
        (
            f"[startup] Consuming one-time code via http://localhost:41221/authenticate?one_time_code={_ONE_TIME_CODE}",
            "[startup] Consuming one-time code via http://localhost:41221/authenticate?one_time_code=***",
        ),
        (
            "Forward stream payload login_url: {'type': 'login_url', "
            f"'url': 'https://localhost:8421/login?one_time_code={_ONE_TIME_CODE}'}}",
            "Forward stream payload login_url: {'type': 'login_url', "
            "'url': 'https://localhost:8421/login?one_time_code=***'}",
        ),
        (
            f"GET /authenticate?one_time_code={_ONE_TIME_CODE}&next=%2F HTTP/1.1",
            "GET /authenticate?one_time_code=***&next=%2F HTTP/1.1",
        ),
        (
            f"\x1b[38;5;33mImbue Studio login URL (one-time use): http://localhost:41221/login?one_time_code={_ONE_TIME_CODE}\x1b[0m",
            "Imbue Studio login URL (one-time use): http://localhost:41221/login?one_time_code=***",
        ),
        ("\x1b[1mordinary chatter\x1b[0m", "ordinary chatter"),
    ],
)
def test_a_quoted_output_line_carries_no_one_time_login_code_and_no_terminal_styling(
    line: str, expected_line: str
) -> None:
    assert _clean_electron_output_line(line) == expected_line


def test_a_one_time_login_code_electron_writes_reaches_neither_kind_of_kept_line() -> None:
    stream = io.BytesIO(
        f"[startup] Consuming one-time code via http://localhost:41221/authenticate?one_time_code={_ONE_TIME_CODE}\n".encode()
    )
    output_tail = _ElectronOutputTail()

    _drain_byte_stream_to_loguru(stream, "electron-out", output_tail)

    reading = output_tail.read()
    kept_line = (
        "[electron-out] [startup] Consuming one-time code via http://localhost:41221/authenticate?one_time_code=***"
    )
    assert reading.startup_lines == (kept_line,)
    assert reading.recent_lines == (kept_line,)


@pytest.mark.parametrize(
    ("backend_lines", "expected_start"),
    [
        ((), _LatchkeyForwardStart.UNREPORTED),
        (("mngr latchkey forward supervisor running (pid=4374)",), _LatchkeyForwardStart.RUNNING),
        (
            (
                "WARNING: Could not start the mngr latchkey forward supervisor: Spawned 'mngr latchkey forward' "
                "(pid=1474) did not take ownership of /tmp/state/latchkey within 30.0s",
            ),
            _LatchkeyForwardStart.FAILED,
        ),
        (
            (
                "WARNING: Could not start the mngr latchkey forward supervisor: stale owner",
                "mngr latchkey forward supervisor running (pid=4374)",
            ),
            _LatchkeyForwardStart.RUNNING,
        ),
    ],
)
def test_the_output_tail_reports_the_latest_the_backend_said_about_starting_its_latchkey_forward(
    backend_lines: tuple[str, ...], expected_start: _LatchkeyForwardStart
) -> None:
    output_tail = _ElectronOutputTail()
    output_tail.record("electron-out", "[startup] Backend ready at http://localhost:41221")
    for backend_line in backend_lines:
        output_tail.record("electron-err", backend_line)
    output_tail.record("electron-err", "chatter after it")

    assert output_tail.read().latchkey_forward_start == expected_start


def test_an_attach_failure_quotes_the_launch_s_output_and_says_the_backend_reported_ready() -> None:
    output_tail = _ElectronOutputTail()
    output_tail.record("electron-out", "[startup] Loading shell.html...")
    output_tail.record("electron-err", "Starting the desktop client on port 41221")
    output_tail.record("electron-out", "[startup] Backend ready at http://localhost:41221")

    error = _ElectronConnectError(_ATTACH_FAILURE_CAUSE, output_tail.read())

    assert str(error).splitlines() == [
        _ATTACH_FAILURE_CAUSE,
        "Backend-ready line '[startup] Backend ready at': seen (this launch wrote 3 lines)",
        "----- main.js's [startup] lines (the newest 2) -----",
        "[electron-out] [startup] Loading shell.html...",
        "[electron-out] [startup] Backend ready at http://localhost:41221",
        "----- the last 3 of the 3 lines Electron wrote to stdout and stderr -----",
        "[electron-out] [startup] Loading shell.html...",
        "[electron-err] Starting the desktop client on port 41221",
        "[electron-out] [startup] Backend ready at http://localhost:41221",
        "----- end of Electron output -----",
    ]


def test_an_attach_failure_says_so_when_the_backend_never_reported_ready() -> None:
    output_tail = _ElectronOutputTail()
    output_tail.record("electron-out", "[startup] Loading shell.html...")
    output_tail.record("electron-err", "[startup] env-setup failed: uv sync exited with code 1")

    error = _ElectronConnectError(_ATTACH_FAILURE_CAUSE, output_tail.read())

    assert str(error).splitlines()[:2] == [
        _ATTACH_FAILURE_CAUSE,
        "Backend-ready line '[startup] Backend ready at': NOT seen (this launch wrote 2 lines)",
    ]
    assert "[electron-err] [startup] env-setup failed: uv sync exited with code 1" in str(error).splitlines()


def test_an_attach_failure_on_a_launch_that_wrote_nothing_says_that() -> None:
    error = _ElectronConnectError(_ATTACH_FAILURE_CAUSE, _ElectronOutputTail().read())

    assert str(error).splitlines() == [
        _ATTACH_FAILURE_CAUSE,
        "Backend-ready line '[startup] Backend ready at': NOT seen (this launch wrote 0 lines)",
        "----- main.js's [startup] lines (the newest 0) -----",
        "----- the last 0 of the 0 lines Electron wrote to stdout and stderr -----",
        "----- end of Electron output -----",
    ]


def test_spent_launch_attempts_give_every_attempt_s_startup_lines_and_the_last_attempt_s_recent_output() -> None:
    never_ready_tail = _ElectronOutputTail()
    never_ready_tail.record("electron-out", "[startup] Loading shell.html...")
    never_ready_tail.record("electron-err", "chatter of the first attempt")
    ready_tail = _ElectronOutputTail()
    ready_tail.record("electron-out", "[startup] Backend ready at http://localhost:41221")
    ready_tail.record("electron-err", "chatter of the last attempt")
    errors = [
        _ElectronConnectError("Electron CDP attach failed on port 48653: first", never_ready_tail.read()),
        _ElectronConnectError("Electron CDP attach failed on port 50112: last", ready_tail.read()),
    ]

    assert _describe_spent_launch_attempts(errors).splitlines() == [
        "No Electron launch could be driven after 2 attempts "
        "(last error: Electron CDP attach failed on port 50112: last)",
        "Attempt 1 of 2: Electron CDP attach failed on port 48653: first",
        "Backend-ready line '[startup] Backend ready at': NOT seen (this launch wrote 2 lines)",
        "----- main.js's [startup] lines (the newest 1) -----",
        "[electron-out] [startup] Loading shell.html...",
        "Attempt 2 of 2: Electron CDP attach failed on port 50112: last",
        "Backend-ready line '[startup] Backend ready at': seen (this launch wrote 2 lines)",
        "----- main.js's [startup] lines (the newest 1) -----",
        "[electron-out] [startup] Backend ready at http://localhost:41221",
        "----- the last 2 of the 2 lines Electron wrote to stdout and stderr -----",
        "[electron-out] [startup] Backend ready at http://localhost:41221",
        "[electron-err] chatter of the last attempt",
        "----- end of Electron output -----",
    ]


def test_the_electron_env_prefills_the_template_only_when_one_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDS_WORKSPACE_GIT_URL", raising=False)
    monkeypatch.delenv("MINDS_USE_LOCAL_WORKSPACE_DEFAULTS", raising=False)

    with_template = _build_electron_env(Path("/tmp/template"), {"STUDIO_ROOTS_OVERRIDE": "/tmp/data-home"})
    assert with_template["MINDS_WORKSPACE_GIT_URL"] == "/tmp/template"
    assert with_template["MINDS_USE_LOCAL_WORKSPACE_DEFAULTS"] == "1"
    assert with_template["STUDIO_ROOTS_OVERRIDE"] == "/tmp/data-home"

    without_template = _build_electron_env(None, {"STUDIO_ROOTS_OVERRIDE": "/tmp/data-home"})
    assert "MINDS_WORKSPACE_GIT_URL" not in without_template
    assert "MINDS_USE_LOCAL_WORKSPACE_DEFAULTS" not in without_template
    assert without_template["MNGR_ROOT_NAME"] == "mngr"


# What a workspace's row on the home page shows in each state, in the shape the page reports it.
_RUNNING_ROW: Mapping[str, object] = {"badge_label": None, "is_stop_offered": True, "is_start_offered": False}
_STOPPING_ROW: Mapping[str, object] = {
    "badge_label": "Stopping\u2026",
    "is_stop_offered": False,
    "is_start_offered": False,
}
_STOPPED_ROW: Mapping[str, object] = {"badge_label": "Stopped", "is_stop_offered": False, "is_start_offered": True}
_STARTING_ROW: Mapping[str, object] = {
    "badge_label": "Starting\u2026",
    "is_stop_offered": False,
    "is_start_offered": False,
}
_HOME_ROW_SELECTOR = f'[data-agent-id="{_WORKSPACE_AGENT_ID}"]'
_STOP_QUESTION = 'Stop "smoke"? Its agents will stop and its services become inaccessible.'


class _FakeDialog:
    def __init__(self, dialog_type: str, message: str) -> None:
        self.type = dialog_type
        self.message = message
        self.is_accepted = False

    def accept(self) -> None:
        self.is_accepted = True


class _FakeHomePage:
    """The account's home page with one workspace row, as the Stop and Start helpers drive it.

    A press on Stop asks its question through a ``confirm`` dialog, raised at whatever dialog listeners
    are on; with none on the question goes unanswered and the press changes nothing, which is what
    Playwright's default dismissal does to it. ``row_states`` is what the row shows on each read after a
    control took effect (the final entry repeats; an exception is raised, as a read that lands while the
    page is being replaced does); before that it shows ``initial_row_state``.
    ``alert_on_wait_number`` raises ``alert_message`` at the listeners during that wait, the way a build
    that reports a refused Stop or Start through a native alert does once its request comes back;
    ``error_dialog_on_wait_number`` puts ``error_dialog_text`` up in the page's own error dialog instead.
    """

    def __init__(
        self,
        *,
        initial_row_state: Mapping[str, object] | None,
        row_states: Sequence[Mapping[str, object] | BaseException | None],
        alert_on_wait_number: int | None = None,
        alert_message: str = "",
        error_dialog_on_wait_number: int | None = None,
        error_dialog_text: str = "",
    ) -> None:
        self._current_states: list[Mapping[str, object] | BaseException | None] = [initial_row_state]
        self._states_after_press = list(row_states)
        self._alert_on_wait_number = alert_on_wait_number
        self._alert_message = alert_message
        self._error_dialog_on_wait_number = error_dialog_on_wait_number
        self._error_dialog_text = error_dialog_text
        self._shown_error_dialog_text: str | None = None
        self.dialog_listeners: list[Callable[[_FakeDialog], None]] = []
        self.dialogs: list[_FakeDialog] = []
        self.clicks: list[tuple[str, object]] = []
        self.visited_urls: list[str] = []
        self.wait_for_timeout_calls = 0

    def on(self, event: str, listener: Callable[[_FakeDialog], None]) -> None:
        assert event == "dialog"
        self.dialog_listeners.append(listener)

    def remove_listener(self, event: str, listener: Callable[[_FakeDialog], None]) -> None:
        assert event == "dialog"
        self.dialog_listeners.remove(listener)

    def goto(self, url: str, wait_until: str) -> None:
        self.visited_urls.append(url)

    def _raise_dialog(self, dialog_type: str, message: str) -> _FakeDialog:
        dialog = _FakeDialog(dialog_type, message)
        self.dialogs.append(dialog)
        for listener in list(self.dialog_listeners):
            listener(dialog)
        return dialog

    def click(self, selector: str, timeout: float, position: object = None) -> None:
        self.clicks.append((selector, position))
        if selector.endswith(_STOP_WORKSPACE_CONTROL_SELECTOR):
            is_confirmed = self._raise_dialog("confirm", _STOP_QUESTION).is_accepted
        else:
            is_confirmed = True
        if is_confirmed:
            self._current_states = list(self._states_after_press)

    def evaluate(self, expression: str, arg: object) -> Mapping[str, object]:
        state = self._current_states.pop(0) if len(self._current_states) > 1 else self._current_states[0]
        if isinstance(state, BaseException):
            raise state
        return {"row": state, "error_dialog_text": self._shown_error_dialog_text}

    def wait_for_timeout(self, timeout_ms: float) -> None:
        self.wait_for_timeout_calls += 1
        if self.wait_for_timeout_calls == self._alert_on_wait_number:
            self._raise_dialog("alert", self._alert_message)
        if self.wait_for_timeout_calls == self._error_dialog_on_wait_number:
            self._shown_error_dialog_text = self._error_dialog_text


def test_a_row_reads_as_running_only_with_no_badge_and_stop_on_offer() -> None:
    assert is_home_row_running(HomeRowState.model_validate(_RUNNING_ROW)) is True
    assert is_home_row_running(HomeRowState.model_validate(_STARTING_ROW)) is False
    assert is_home_row_running(HomeRowState.model_validate(_STOPPED_ROW)) is False
    # A row that shows neither a badge nor a control is one whose machine state the app does not know.
    unknown = HomeRowState(badge_label=None, is_stop_offered=False, is_start_offered=False)
    assert is_home_row_running(unknown) is False


def test_a_row_reads_as_stopped_only_with_the_stopped_badge_and_start_on_offer() -> None:
    assert is_home_row_stopped(HomeRowState.model_validate(_STOPPED_ROW)) is True
    assert is_home_row_stopped(HomeRowState.model_validate(_STOPPING_ROW)) is False
    assert is_home_row_stopped(HomeRowState.model_validate(_RUNNING_ROW)) is False
    # A stop an operator holds reads "Maintenance" and offers no Start.
    held = HomeRowState(badge_label="Maintenance", is_stop_offered=False, is_start_offered=False)
    assert is_home_row_stopped(held) is False


def test_the_home_page_of_an_account_is_its_hub_route() -> None:
    page = _FakeHomePage(initial_row_state=_RUNNING_ROW, row_states=[_RUNNING_ROW])
    open_account_home_page(cast(Page, page), "http://localhost:8420", "local")
    assert page.visited_urls == ["http://localhost:8420/account/local/"]


def test_stopping_from_the_home_page_answers_the_question_and_returns_once_the_row_reads_stopped() -> None:
    page = _FakeHomePage(initial_row_state=_RUNNING_ROW, row_states=[_STOPPING_ROW, _STOPPING_ROW, _STOPPED_ROW])

    stop_workspace_from_home_page(cast(Page, page), _WORKSPACE_AGENT_ID, timeout_seconds=5)

    assert page.clicks == [(f"{_HOME_ROW_SELECTOR} {_STOP_WORKSPACE_CONTROL_SELECTOR}", None)]
    assert [(dialog.type, dialog.is_accepted) for dialog in page.dialogs] == [("confirm", True)]
    assert page.wait_for_timeout_calls == 2
    # Left on, the listener would go on answering every later dialog of the page.
    assert page.dialog_listeners == []


def test_a_refused_stop_raises_with_what_the_home_page_reported() -> None:
    page = _FakeHomePage(
        initial_row_state=_RUNNING_ROW,
        row_states=[_STOPPING_ROW, _RUNNING_ROW],
        alert_on_wait_number=1,
        alert_message='Could not stop "smoke". Check the workspace\'s provider and try again.',
    )

    with pytest.raises(WorkspaceFlowError, match='Could not stop "smoke"'):
        stop_workspace_from_home_page(cast(Page, page), _WORKSPACE_AGENT_ID, timeout_seconds=5)

    assert page.dialog_listeners == []


def test_a_refused_stop_raises_with_what_the_app_s_own_error_dialog_says() -> None:
    page = _FakeHomePage(
        initial_row_state=_RUNNING_ROW,
        row_states=[_STOPPING_ROW, _RUNNING_ROW],
        error_dialog_on_wait_number=1,
        error_dialog_text='Couldn\'t stop "smoke" The machine did not answer. Try again Report a bug',
    )

    with pytest.raises(WorkspaceFlowError, match='Couldn\'t stop "smoke" The machine did not answer'):
        stop_workspace_from_home_page(cast(Page, page), _WORKSPACE_AGENT_ID, timeout_seconds=5)

    assert page.dialog_listeners == []


def test_a_stop_that_never_lands_raises_naming_what_the_row_last_showed() -> None:
    page = _FakeHomePage(initial_row_state=_RUNNING_ROW, row_states=[_STOPPING_ROW])

    with pytest.raises(WorkspaceFlowError, match="Stopping"):
        stop_workspace_from_home_page(cast(Page, page), _WORKSPACE_AGENT_ID, timeout_seconds=0)

    assert page.dialog_listeners == []


def test_a_row_read_that_lands_while_the_page_is_replaced_is_taken_again() -> None:
    # A stop has the app move its windows off the stopped workspace, which can replace the page under a read.
    page = _FakeHomePage(
        initial_row_state=_RUNNING_ROW,
        row_states=[_STOPPING_ROW, PlaywrightError("Execution context was destroyed"), _STOPPED_ROW],
    )

    stop_workspace_from_home_page(cast(Page, page), _WORKSPACE_AGENT_ID, timeout_seconds=5)

    assert page.wait_for_timeout_calls == 2


def test_a_stop_whose_row_leaves_the_page_raises_saying_so() -> None:
    page = _FakeHomePage(initial_row_state=_RUNNING_ROW, row_states=[None])

    with pytest.raises(WorkspaceFlowError, match="no row"):
        stop_workspace_from_home_page(cast(Page, page), _WORKSPACE_AGENT_ID, timeout_seconds=0)


def test_stop_is_not_pressed_while_the_row_does_not_read_running() -> None:
    page = _FakeHomePage(initial_row_state=_STARTING_ROW, row_states=[_STOPPED_ROW])

    with pytest.raises(WorkspaceFlowError, match="was not running .*Starting"):
        stop_workspace_from_home_page(cast(Page, page), _WORKSPACE_AGENT_ID, timeout_seconds=0)

    assert page.clicks == []
    assert page.dialog_listeners == []


def test_start_is_not_pressed_while_the_row_does_not_read_stopped() -> None:
    page = _FakeHomePage(initial_row_state=_STOPPING_ROW, row_states=[_RUNNING_ROW])

    with pytest.raises(WorkspaceFlowError, match="was not stopped .*Stopping"):
        start_workspace_from_home_page(cast(Page, page), _WORKSPACE_AGENT_ID, timeout_seconds=0)

    assert page.clicks == []
    assert page.dialog_listeners == []


def test_starting_from_the_home_page_returns_once_the_row_shows_no_badge_and_offers_stop() -> None:
    page = _FakeHomePage(initial_row_state=_STOPPED_ROW, row_states=[_STARTING_ROW, _RUNNING_ROW])

    start_workspace_from_home_page(cast(Page, page), _WORKSPACE_AGENT_ID, timeout_seconds=5)

    assert page.clicks == [(f"{_HOME_ROW_SELECTOR} {_START_WORKSPACE_CONTROL_SELECTOR}", None)]
    assert page.dialogs == []
    assert page.wait_for_timeout_calls == 1
    assert page.dialog_listeners == []


def test_a_refused_start_raises_with_what_the_home_page_reported() -> None:
    page = _FakeHomePage(
        initial_row_state=_STOPPED_ROW,
        row_states=[_STARTING_ROW, _STOPPED_ROW],
        alert_on_wait_number=1,
        alert_message='Could not start "smoke". Check the workspace\'s provider and try again.',
    )

    with pytest.raises(WorkspaceFlowError, match='Could not start "smoke"'):
        start_workspace_from_home_page(cast(Page, page), _WORKSPACE_AGENT_ID, timeout_seconds=5)

    assert page.dialog_listeners == []


class _FakeDesktopFrame(_FakeFrame):
    """A workspace's frame whose desktop backdrop turns visible on a given read (the final entry repeats)."""

    def __init__(self, url: str, is_backdrop_visible_results: Sequence[bool | BaseException]) -> None:
        super().__init__([url])
        self._is_backdrop_visible_results = list(is_backdrop_visible_results)

    def is_visible(self, selector: str) -> bool:
        assert selector == DESKTOP_BACKDROP_SELECTOR
        results = self._is_backdrop_visible_results
        result = results.pop(0) if len(results) > 1 else results[0]
        if isinstance(result, BaseException):
            raise result
        return result


class _FakeHomePageWithWorkspaceFrame(_FakeHomePage):
    """The home page whose row, once pressed, has the chrome page frame the workspace after some waits."""

    def __init__(self, *, workspace_frame: _FakeDesktopFrame, frame_attaches_after_wait_number: int) -> None:
        super().__init__(initial_row_state=_RUNNING_ROW, row_states=[_RUNNING_ROW])
        self._workspace_frame = workspace_frame
        self._frame_attaches_after_wait_number = frame_attaches_after_wait_number
        self.url = "http://localhost:8420/account/local/"

    @property
    def frames(self) -> list[_FakeFrame]:
        if self.clicks and self.wait_for_timeout_calls >= self._frame_attaches_after_wait_number:
            return [self._workspace_frame]
        return []


def test_entering_from_the_home_page_presses_the_row_and_returns_its_frame_once_the_desktop_renders() -> None:
    # The frame is on the page one wait after the press, and its first read lands while its document is
    # being replaced; the desktop is up on the read after that.
    frame = _FakeDesktopFrame(_WORKSPACE_SHELL_URL, [PlaywrightError("Execution context was destroyed"), False, True])
    page = _FakeHomePageWithWorkspaceFrame(workspace_frame=frame, frame_attaches_after_wait_number=1)

    entered = enter_workspace_from_home_page(cast(Page, page), _WORKSPACE_AGENT_ID, timeout_seconds=5)

    assert entered is cast(Frame, frame)
    assert [selector for selector, _position in page.clicks] == [_HOME_ROW_SELECTOR]
    # The press lands on the row itself rather than on one of the controls at its far end.
    assert page.clicks[0][1] is not None
    assert page.wait_for_timeout_calls == 3


def test_entering_raises_when_the_desktop_never_renders() -> None:
    frame = _FakeDesktopFrame(_WORKSPACE_SHELL_URL, [False])
    page = _FakeHomePageWithWorkspaceFrame(workspace_frame=frame, frame_attaches_after_wait_number=0)

    with pytest.raises(WorkspaceFlowError, match="did not render"):
        enter_workspace_from_home_page(cast(Page, page), _WORKSPACE_AGENT_ID, timeout_seconds=0)


class _FakeChatApiPage:
    """A chat page whose requests to the chat app's API get scripted answers, recorded in the order made.

    ``answers_by_request`` maps ``(method, path)`` to the answers that request gets, one per request; the
    final entry repeats.
    """

    def __init__(self, url: str, answers_by_request: Mapping[tuple[str, str], Sequence[Mapping[str, object]]]) -> None:
        self.url = url
        self._answers_by_request = {request: list(answers) for request, answers in answers_by_request.items()}
        self.requests: list[tuple[str, str, object]] = []
        self.wait_for_timeout_calls = 0

    def evaluate(self, expression: str, arg: Mapping[str, object]) -> Mapping[str, object]:
        method, path = str(arg["method"]), str(arg["path"])
        self.requests.append((method, path, arg["body"]))
        answers = self._answers_by_request[(method, path)]
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def wait_for_timeout(self, timeout_ms: float) -> None:
        self.wait_for_timeout_calls += 1


_MODEL_OPTIONS_REQUEST = ("GET", f"/api/chats/{_WELCOME_CHAT_ID}/model-options")
_MODEL_PICK_REQUEST = ("POST", f"/api/chats/{_WELCOME_CHAT_ID}/model")
_EVENTS_REQUEST = ("GET", f"/api/chats/{_WELCOME_CHAT_ID}/events")
_CHAT_NOT_FOUND = {"status": 404, "body": '{"error": "Chat not found"}'}
_OK = {"status": 200, "body": '{"status": "ok"}'}


def test_switching_to_the_cheapest_model_waits_for_the_chat_s_agent_then_posts_the_picker_s_haiku_pick() -> None:
    # The chat app knows no agent for the chat until its create lands, so its model options are a 404 until then.
    chat = _FakeChatApiPage(
        _WELCOME_CHAT_PAGE_URL,
        {
            _MODEL_OPTIONS_REQUEST: [_CHAT_NOT_FOUND, _CHAT_NOT_FOUND, {"status": 200, "body": '{"models": null}'}],
            _MODEL_PICK_REQUEST: [_OK],
        },
    )

    switch_chat_to_cheapest_model(cast(Frame, chat), agent_up_timeout_seconds=5, label="test")

    assert [(method, path) for method, path, _body in chat.requests] == [
        _MODEL_OPTIONS_REQUEST,
        _MODEL_OPTIONS_REQUEST,
        _MODEL_OPTIONS_REQUEST,
        _MODEL_PICK_REQUEST,
    ]
    assert chat.requests[-1][2] == {
        "model_id": "haiku",
        "effort": "low",
        "fast": False,
        "axes": ["model", "effort"],
    }
    assert chat.wait_for_timeout_calls == 2


def test_switching_to_the_cheapest_model_raises_when_the_chat_never_gets_an_agent() -> None:
    chat = _FakeChatApiPage(_WELCOME_CHAT_PAGE_URL, {_MODEL_OPTIONS_REQUEST: [_CHAT_NOT_FOUND]})

    with pytest.raises(WorkspaceFlowError, match="had no agent to pick a model for"):
        switch_chat_to_cheapest_model(cast(Frame, chat), agent_up_timeout_seconds=0, label="test")

    assert _MODEL_PICK_REQUEST not in [(method, path) for method, path, _body in chat.requests]


def test_switching_to_the_cheapest_model_raises_with_the_chat_s_refusal() -> None:
    refusal = {"status": 400, "body": '{"error": "This model requires an effort level"}'}
    chat = _FakeChatApiPage(
        _WELCOME_CHAT_PAGE_URL,
        {_MODEL_OPTIONS_REQUEST: [{"status": 200, "body": '{"models": null}'}], _MODEL_PICK_REQUEST: [refusal]},
    )

    with pytest.raises(WorkspaceFlowError, match="refused the model pick .* with 400: .*requires an effort level"):
        switch_chat_to_cheapest_model(cast(Frame, chat), agent_up_timeout_seconds=5, label="test")


def test_switching_to_the_cheapest_model_raises_on_a_page_that_is_not_a_chat() -> None:
    chat = _FakeChatApiPage(_TERMINAL_PAGE_URL, {})

    with pytest.raises(WorkspaceFlowError, match="Not a chat page"):
        switch_chat_to_cheapest_model(cast(Frame, chat), agent_up_timeout_seconds=5, label="test")

    assert chat.requests == []


_TOKEN = "q7r2x9z4"
_SEEDED_WELCOME = {"type": "assistant_message", "text": "Welcome! Pick a provider to start.", "model": "seed"}
_USER_TURN = {"type": "user_message", "text": f"Reply with exactly this token and nothing else: {_TOKEN}"}
_REPLY = {"type": "assistant_message", "text": _TOKEN, "model": "claude-haiku-5-5"}


def test_the_model_of_a_reply_is_the_one_its_assistant_message_names_and_not_the_user_turn_or_the_seed() -> None:
    assert model_of_assistant_reply_containing([_SEEDED_WELCOME, _USER_TURN, _REPLY], _TOKEN) == "claude-haiku-5-5"


def test_the_model_of_a_reply_is_none_while_no_assistant_message_carries_the_token() -> None:
    assert model_of_assistant_reply_containing([_SEEDED_WELCOME, _USER_TURN], _TOKEN) is None
    assert model_of_assistant_reply_containing([], _TOKEN) is None


def test_reading_the_model_of_a_chat_reply_reads_the_chat_s_events() -> None:
    events = {"events": [_SEEDED_WELCOME, _USER_TURN, _REPLY], "offset": 0, "total": 3}
    chat = _FakeChatApiPage(_WELCOME_CHAT_PAGE_URL, {_EVENTS_REQUEST: [{"status": 200, "body": json.dumps(events)}]})

    assert read_model_of_chat_reply(cast(Frame, chat), _TOKEN) == "claude-haiku-5-5"


def test_reading_the_model_of_a_chat_reply_raises_when_the_transcript_has_no_such_reply() -> None:
    events = {"events": [_SEEDED_WELCOME, _USER_TURN], "offset": 0, "total": 2}
    chat = _FakeChatApiPage(_WELCOME_CHAT_PAGE_URL, {_EVENTS_REQUEST: [{"status": 200, "body": json.dumps(events)}]})

    with pytest.raises(WorkspaceFlowError, match="holds no assistant message naming a model"):
        read_model_of_chat_reply(cast(Frame, chat), _TOKEN)
