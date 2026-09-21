import re
from collections.abc import Sequence
from typing import cast

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from imbue.minds.desktop_client.e2e_workspace_runner import WorkspaceCreateAttemptFailedError
from imbue.minds.desktop_client.e2e_workspace_runner import WorkspaceFlowError
from imbue.minds.desktop_client.e2e_workspace_runner import _LAUNCHER_FIELD_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import _LAUNCHER_INPUT_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import _LAUNCHER_OVERLAY_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import _NEW_CHAT_TILE_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import _NEW_TERMINAL_TILE_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import _TERMINAL_IFRAME_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import _chat_frame
from imbue.minds.desktop_client.e2e_workspace_runner import _read_failure_message
from imbue.minds.desktop_client.e2e_workspace_runner import _wait_for_workspace_ready_or_failure
from imbue.minds.desktop_client.e2e_workspace_runner import open_terminal_from_launcher
from imbue.minds.desktop_client.e2e_workspace_runner import start_new_chat_from_launcher

# A workspace-ready URL (matches the agent-subdomain pattern) and a still-pending
# backend URL (does not), used to drive the waiter's success/failure branches.
_READY_URL = "http://host-0123456789abcdef0123456789abcdef.localhost:8080/"
_PENDING_URL = "http://localhost:8080/create"


class _FakeElement:
    def __init__(self, text: str) -> None:
        self._text = text

    def inner_text(self) -> str:
        return self._text


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
        return None

    def wait_for_timeout(self, timeout_ms: float) -> None:
        self.wait_for_timeout_calls += 1


def test_wait_returns_the_content_page_that_reached_the_workspace() -> None:
    workspace = _FakeContentPage(urls=[_READY_URL])
    creating = _FakeCreatingPage(urls=[_PENDING_URL], is_visible_results=[False], candidate_pages=[workspace])
    # Returns the workspace frame once its agent-subdomain URL is reached (the
    # chrome page that drove the form -- ``creating`` -- stays on /create-ish).
    result = _wait_for_workspace_ready_or_failure(
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
    result = _wait_for_workspace_ready_or_failure(
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
        error_message="unknown or invalid runtime name: runsc",
    )
    with pytest.raises(WorkspaceCreateAttemptFailedError) as exc_info:
        _wait_for_workspace_ready_or_failure(cast(Browser, creating.browser), cast(Page, creating), timeout_seconds=5)
    # The surfaced error text rides along so the failure is diagnosable.
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
    result = _wait_for_workspace_ready_or_failure(
        cast(Browser, creating.browser), cast(Page, creating), timeout_seconds=5
    )
    assert result is cast(Frame, workspace.main_frame)
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


def _terminal_selector_prefixes() -> list[str]:
    """The ``src^="..."`` prefix literals the terminal-iframe selector keys on."""
    return re.findall(r'src\^="([^"]+)"', _TERMINAL_IFRAME_SELECTOR)


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
# The welcome chat the creation page seeded, which a fresh workspace opens on before any tile is pressed.
_WELCOME_CHAT_ID = "agent-0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"
_WELCOME_CHAT_PAGE_URL = f"https://chat-x7k9q2w1.{_WORKSPACE_AGENT_ID}.localhost:8421/{_WELCOME_CHAT_ID}"
_TERMINAL_PAGE_URL = f"https://terminal-x7k9q2w1.{_WORKSPACE_AGENT_ID}.localhost:8421/"


class _FakeWorkspaceFrame:
    """The workspace frame: ``_chat_frame`` scans the frames under it and polls with ``wait_for_timeout``.

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
    assert _chat_frame(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, chat)
    assert workspace.wait_for_timeout_calls == 0


def test_chat_frame_is_found_under_the_chat_root_the_desktop_frames() -> None:
    # The desktop frames the chat app's root (path ``/``), and the root frames the chat: the chat
    # page is the workspace's grandchild, and the root itself is not a chat page.
    chat = _FakeFrame(urls=[_CHAT_PAGE_URL])
    root = _FakeFrame(urls=[f"https://chat-x7k9q2w1.{_WORKSPACE_AGENT_ID}.localhost:8421/"], child_frames=[chat])
    workspace = _FakeWorkspaceFrame(child_frame_lists=[[root]])
    assert _chat_frame(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, chat)


def test_chat_frame_polls_until_the_chat_attaches() -> None:
    # The shell frames the chat's page after the tile is pressed, so it is not
    # there on the first scan; the poll runs through Playwright's own wait.
    chat = _FakeFrame(urls=[_CHAT_PAGE_URL])
    workspace = _FakeWorkspaceFrame(child_frame_lists=[[], [chat]])
    assert _chat_frame(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, chat)
    assert workspace.wait_for_timeout_calls == 1


def test_chat_frame_ignores_the_agent_id_in_a_host_name() -> None:
    # Only a URL PATH ending in the agent id is a chat page: the workspace shell
    # and its service iframes carry the agent id in their host names, on path ``/``.
    workspace = _FakeWorkspaceFrame(child_frame_lists=[[_FakeFrame(urls=[_WORKSPACE_SHELL_URL])]])
    with pytest.raises(WorkspaceFlowError):
        _chat_frame(cast(Frame, workspace), timeout_seconds=0)


def test_chat_frame_raises_when_no_chat_opens_in_time() -> None:
    workspace = _FakeWorkspaceFrame(child_frame_lists=[[_FakeFrame(urls=[_TERMINAL_PAGE_URL])]])
    with pytest.raises(WorkspaceFlowError):
        _chat_frame(cast(Frame, workspace), timeout_seconds=0)


class _FakeLauncherWorkspace(_FakeWorkspaceFrame):
    """A workspace frame under the desktop's launcher contract.

    The launcher opens from the taskbar's search field (its input, or the field itself where the
    compact layout renders it as a button: ``has_launcher_input``) and its tiles are in the DOM
    only while its overlay shows; a tile wait succeeds only when it is scoped to the showing
    overlay, the way the runner issues it. Records the selectors clicked; ``existing_chats`` are open from the start (the welcome chat a
    fresh workspace opens on), and the chat frame joins them only after the New Chat tile is
    pressed, the way the shell frames it.
    """

    def __init__(
        self,
        *,
        is_launcher_showing: bool,
        existing_chats: Sequence[_FakeFrame],
        chat: _FakeFrame,
        has_launcher_input: bool = True,
    ) -> None:
        super().__init__(child_frame_lists=[list(existing_chats)])
        self._is_launcher_showing = is_launcher_showing
        self._has_launcher_input = has_launcher_input
        self._existing_chats = list(existing_chats)
        self._chat = chat
        self.clicked: list[str] = []

    def query_selector(self, selector: str) -> object | None:
        if selector == f"{_LAUNCHER_OVERLAY_SELECTOR}:visible":
            return object() if self._is_launcher_showing else None
        if selector == _LAUNCHER_INPUT_SELECTOR:
            return object() if self._has_launcher_input else None
        return None

    def wait_for_selector(self, selector: str, state: str, timeout: float) -> None:
        if not selector.startswith(f"{_LAUNCHER_OVERLAY_SELECTOR}:visible ") or not self._is_launcher_showing:
            raise PlaywrightTimeoutError(f"no visible launcher carries a tile matching {selector!r}")

    def click(self, selector: str) -> None:
        self.clicked.append(selector)
        if selector in (_LAUNCHER_INPUT_SELECTOR, _LAUNCHER_FIELD_SELECTOR):
            self._is_launcher_showing = True
        if selector == f"{_LAUNCHER_OVERLAY_SELECTOR}:visible {_NEW_CHAT_TILE_SELECTOR}":
            self._child_frame_lists = [[*self._existing_chats, self._chat]]


class _FakeTerminalLauncherWorkspace(_FakeLauncherWorkspace):
    """The launcher fake for the terminal tile: records what it waits for, since the terminal's page is a plain iframe."""

    def __init__(self, *, is_launcher_showing: bool) -> None:
        super().__init__(is_launcher_showing=is_launcher_showing, existing_chats=[], chat=_FakeFrame(urls=[]))
        self.waited_for: list[str] = []

    def wait_for_selector(self, selector: str, state: str, timeout: float) -> None:
        self.waited_for.append(selector)


_VISIBLE_NEW_CHAT_TILE_SELECTOR = f"{_LAUNCHER_OVERLAY_SELECTOR}:visible {_NEW_CHAT_TILE_SELECTOR}"
_VISIBLE_NEW_TERMINAL_TILE_SELECTOR = f"{_LAUNCHER_OVERLAY_SELECTOR}:visible {_NEW_TERMINAL_TILE_SELECTOR}"


def test_open_terminal_presses_the_terminal_tile_and_waits_for_its_frame() -> None:
    workspace = _FakeTerminalLauncherWorkspace(is_launcher_showing=True)
    open_terminal_from_launcher(cast(Frame, workspace))
    assert workspace.clicked == [_VISIBLE_NEW_TERMINAL_TILE_SELECTOR]
    assert workspace.waited_for == [_VISIBLE_NEW_TERMINAL_TILE_SELECTOR, _TERMINAL_IFRAME_SELECTOR]


def test_open_terminal_opens_the_launcher_first_when_it_is_not_showing() -> None:
    workspace = _FakeTerminalLauncherWorkspace(is_launcher_showing=False)
    open_terminal_from_launcher(cast(Frame, workspace))
    assert workspace.clicked == [_LAUNCHER_INPUT_SELECTOR, _VISIBLE_NEW_TERMINAL_TILE_SELECTOR]


def test_start_new_chat_presses_the_tile_and_returns_the_chat_frame_it_opens() -> None:
    chat = _FakeFrame(urls=[_CHAT_PAGE_URL])
    workspace = _FakeLauncherWorkspace(is_launcher_showing=True, existing_chats=[], chat=chat)
    assert start_new_chat_from_launcher(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, chat)
    assert workspace.clicked == [_VISIBLE_NEW_CHAT_TILE_SELECTOR]


def test_start_new_chat_opens_the_launcher_first_when_it_is_not_showing() -> None:
    # A workspace showing only its windows has no tile on screen; the taskbar's search field
    # opens the launcher that carries it.
    chat = _FakeFrame(urls=[_CHAT_PAGE_URL])
    workspace = _FakeLauncherWorkspace(is_launcher_showing=False, existing_chats=[], chat=chat)
    assert start_new_chat_from_launcher(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, chat)
    assert workspace.clicked == [_LAUNCHER_INPUT_SELECTOR, _VISIBLE_NEW_CHAT_TILE_SELECTOR]


def test_start_new_chat_opens_the_launcher_from_the_field_itself_when_it_has_no_input() -> None:
    # The compact layout renders the taskbar's search field as a bare button, with no input to click.
    chat = _FakeFrame(urls=[_CHAT_PAGE_URL])
    workspace = _FakeLauncherWorkspace(
        is_launcher_showing=False, existing_chats=[], chat=chat, has_launcher_input=False
    )
    assert start_new_chat_from_launcher(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, chat)
    assert workspace.clicked == [_LAUNCHER_FIELD_SELECTOR, _VISIBLE_NEW_CHAT_TILE_SELECTOR]


def test_start_new_chat_returns_the_chat_the_tile_opened_and_not_the_welcome_chat_already_open() -> None:
    # A fresh workspace opens on the welcome chat the creation page seeded, so a chat frame is
    # open (and first in frame order) before the tile is pressed; the chat the press starts
    # is the frame that was not there before.
    welcome = _FakeFrame(urls=[_WELCOME_CHAT_PAGE_URL])
    chat = _FakeFrame(urls=[_CHAT_PAGE_URL])
    workspace = _FakeLauncherWorkspace(is_launcher_showing=False, existing_chats=[welcome], chat=chat)
    assert start_new_chat_from_launcher(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, chat)
    assert _chat_frame(cast(Frame, workspace), timeout_seconds=0) is cast(Frame, welcome)


def test_start_new_chat_tells_the_welcome_chat_apart_by_its_id_when_its_url_changes_after_the_press() -> None:
    # The chat page reports its own URL back to the shell, so the welcome chat's frame URL can
    # pick up a query string or trailing slash between the pre-press snapshot and the poll; it
    # is still the same chat, and only the tile-minted one is new.
    welcome = _FakeFrame(urls=[_WELCOME_CHAT_PAGE_URL, f"{_WELCOME_CHAT_PAGE_URL}/?tab=1"])
    chat = _FakeFrame(urls=[_CHAT_PAGE_URL])
    workspace = _FakeLauncherWorkspace(is_launcher_showing=True, existing_chats=[welcome], chat=chat)
    assert start_new_chat_from_launcher(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, chat)


class _FakeLauncherWorkspaceFramingTheWelcomeChatWithTheTile(_FakeLauncherWorkspace):
    """The launcher fake on a workspace still booting: the welcome chat's frame appears at the moment the tile
    comes on screen, since the shell renders both from the same app-list arrival."""

    def __init__(self, *, welcome: _FakeFrame, chat: _FakeFrame) -> None:
        super().__init__(is_launcher_showing=True, existing_chats=[], chat=chat)
        self._welcome = welcome

    def wait_for_selector(self, selector: str, state: str, timeout: float) -> None:
        super().wait_for_selector(selector, state, timeout)
        self._existing_chats = [self._welcome]
        self._child_frame_lists = [[self._welcome]]


def test_start_new_chat_does_not_take_a_welcome_chat_framed_during_the_tile_wait_for_the_new_chat() -> None:
    welcome = _FakeFrame(urls=[_WELCOME_CHAT_PAGE_URL])
    chat = _FakeFrame(urls=[_CHAT_PAGE_URL])
    workspace = _FakeLauncherWorkspaceFramingTheWelcomeChatWithTheTile(welcome=welcome, chat=chat)
    assert start_new_chat_from_launcher(cast(Frame, workspace), timeout_seconds=5) is cast(Frame, chat)


def test_start_new_chat_raises_when_only_the_welcome_chat_is_open_after_the_press() -> None:
    welcome = _FakeFrame(urls=[_WELCOME_CHAT_PAGE_URL])
    workspace = _FakeLauncherWorkspace(is_launcher_showing=True, existing_chats=[welcome], chat=welcome)
    with pytest.raises(WorkspaceFlowError):
        start_new_chat_from_launcher(cast(Frame, workspace), timeout_seconds=0)
