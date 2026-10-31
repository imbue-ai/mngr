"""The journey's steps inside the launched Imbue Studio app, driven through its own UI.

Every control here is the one a user presses: the landing page and its
consent screen, the Manage Accounts page's sign-in, the create form, the
desktop's launcher field, the Share tab, and the Settings tab's destroy.
What the UI did is then confirmed by reading the app's own routes,
through a same-origin fetch on a page it already has open -- the chrome page
for the desktop's routes, the chat's page for the workspace's -- so each
one's session authenticates the call.

Two routes are called rather than driven through the chooser: the desktop's
AI-key mint, and the workspace's adoption of the blob it mints. They are the
routes behind the chooser's "Sign in with Imbue" lane; driving that lane's own
controls is a follow-up recorded in the spec.
"""

import json
import re
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final
from urllib.parse import urlsplit

from loguru import logger
from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame
from playwright.sync_api import Page
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.e2e_workspace_runner import DESKTOP_BACKDROP_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import USER_TURN_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import chat_frame
from imbue.minds.desktop_client.e2e_workspace_runner import find_chat_frame
from imbue.minds.desktop_client.e2e_workspace_runner import send_to_main_chat_from_launcher
from imbue.minds.desktop_client.e2e_workspace_runner import wait_for_workspace_ready_or_failure
from imbue.minds.desktop_client.e2e_workspace_runner import workspace_coordinate_from_subdomain
from imbue.minds.smoke_tests.data_types import StudioAppSession
from imbue.minds.smoke_tests.primitives import SmokeAppError

# The pages a fresh app lands on: the start flow, the consent screen that can
# precede it, the landing list of an install with workspaces, or a workspace.
LANDING_SELECTORS: Final[tuple[str, ...]] = (
    "#start-flow",
    "#consent-continue",
    "#landing-minds-settings",
    "#content-frame",
)
_CONSENT_CONTINUE: Final[str] = "#consent-continue"
_START_FLOW: Final[str] = "#start-flow"
# The start flow (``StartPage.ts``): its answer buttons carry their choice in
# ``data-answer``; the way in for someone who already has a workspace is the
# start titlebar's log-in.
_REPORTING_DECLINE: Final[str] = '[data-answer="reporting-no"]'
_EXISTING_LOGIN_BUTTON: Final[str] = "#start-log-in"
# The Manage Accounts page's one button, and the web-login modal's copy of the sign-in URL.
_ACCOUNTS_PATH: Final[str] = "/accounts"
_ADD_ACCOUNT_BUTTON: Final[str] = 'button:has-text("Add account")'
_WEB_LOGIN_URL: Final[str] = "#web-login-url"
# The modal's Try again, shown when the app could not start its sign-in flow.
_WEB_LOGIN_RETRY: Final[str] = "#web-login-retry-btn"
# A user presses Try again when the flow fails to start; the smoke run does too.
_WEB_LOGIN_START_ATTEMPTS: Final[int] = 3
# The create form, plus the compute preset cards. The form creates under the
# window's account, which is the hub route's segment.
_ACCOUNTS_API_PATH: Final[str] = "/api/v1/accounts"
_CREATE_FORM: Final[str] = "#create-form"
_REMOTE_PRESET: Final[str] = '[data-preset="remote"]'
_ADVANCED_TOGGLE: Final[str] = "#toggle-advanced"
_HOST_NAME_FIELD: Final[str] = "#host_name"
_CREATE_ACCOUNT_LABEL: Final[str] = "#create-account"
_LAUNCH_MODE_SELECT: Final[str] = "#launch_mode"
_REGION_SELECT: Final[str] = "#region"
# The form's "Enable sharing and web access" switch.
_CREATE_SHARING_SWITCH: Final[str] = "#enable_web_access"
_CREATE_SUBMIT: Final[str] = "#create-submit"
_CREATING_PAGE: Final[str] = "#creating"
IMBUE_CLOUD_LAUNCH_MODE: Final[str] = "IMBUE_CLOUD"
# The Share tab (``ShareTab.ts``) and the Settings tab's destroy (``SettingsGroups.ts``).
_SHARE_TAB_PATH: Final[str] = "/workspace/{agent_id}/options?tab=share"
_SETTINGS_TAB_PATH: Final[str] = "/workspace/{agent_id}/options?tab=settings"
_PUBLISH_SWITCH: Final[str] = "#ws-share-publish-switch"
# The switch is a ``role="switch"`` button: it says which way it points, and says
# it is busy while the publish or unpublish it started is still settling.
_PUBLISH_SWITCH_ON: Final[str] = f'{_PUBLISH_SWITCH}[aria-checked="true"]'
_PUBLISH_SWITCH_SETTLED_OFF: Final[str] = f'{_PUBLISH_SWITCH}[aria-checked="false"]:not([aria-busy])'
_PUBLISH_CONFIRM: Final[str] = "#ws-share-publish-confirm"
_PUBLISH_ERROR: Final[str] = "#ws-share-publish-error"
_SHARE_LINK: Final[str] = "#ws-share-link"
_SHARE_COPY: Final[str] = "#ws-share-copy"
_DESTROY_BUTTON: Final[str] = "#destroy-btn"
_DESTROY_CONFIRM: Final[str] = "#destroy-confirm-btn"
# The app's own routes the journey reads to confirm what the UI did.
_WORKSPACES_PATH: Final[str] = "/api/v1/workspaces"
# The desktop's AI-keys mint: the account's LiteLLM key for one workspace, as an env-file blob.
_MINT_AI_KEY_PATH: Final[str] = "/settings/ai-keys/mint"
# The workspace chat app's own route for that blob, and the mode it reports for a key
# paired with a base URL (which is what makes the workspace talk through Imbue's proxy).
_SUBMIT_CREDENTIALS_PATH: Final[str] = "/api/claude-auth/submit-credentials"
_IMBUE_AUTH_MODE: Final[str] = "imbue"
# Its listing of the chats it knows, which names the workspace's main chat.
_CHATS_PATH: Final[str] = "/api/chats"
_SHARING_PATH: Final[str] = "/api/v1/workspace-sharing/{agent_id}"
_SHARING_READINESS_PATH: Final[str] = "/api/v1/workspace-sharing/{agent_id}/readiness"
_DESTROY_OPERATION_PATH: Final[str] = "/api/v1/workspaces/operations/destroy/{agent_id}"

_LANDING_TIMEOUT_MS: Final[float] = 180_000.0
_PAGE_TIMEOUT_MS: Final[float] = 30_000.0
_WEB_LOGIN_URL_TIMEOUT_MS: Final[float] = 60_000.0
# A pre-baked pool host is adopted in a couple of minutes; when none matches
# the app leases any host and rebuilds the workspace on it, which takes ten
# minutes and more.
_CREATE_TIMEOUT_SECONDS: Final[int] = 1200
_DESKTOP_TIMEOUT_MS: Final[float] = 180_000.0
# The welcome chat is framed by the desktop's app list, which arrives after the backdrop.
_CHAT_FRAME_TIMEOUT_SECONDS: Final[float] = 240.0
# The chat app answers its listing only once it has read the agent list from mngr.
_MAIN_CHAT_TIMEOUT_SECONDS: Final[float] = 120.0
# The launcher's text reaches the main chat through the chat app's intake, which the chat's page then sends.
_PROMPT_SHOWN_TIMEOUT_SECONDS: Final[float] = 120.0
# Publishing dials the relay and waits on a certificate before the link shows.
_PUBLISH_TIMEOUT_MS: Final[float] = 300_000.0
_POLL_SECONDS: Final[float] = 2.0

# The transcript only auto-scrolls when it was already at its tail and virtualizes
# off-screen rows, so every read scrolls each ``.app-content`` to its end first.
# The match is on an element whose whole text is the reply, because the class the
# transcript marks a message with varies with the kind of answer.
_SCROLL_CHAT_TO_TAIL_JS: Final[str] = (
    "for (const el of document.querySelectorAll('.app-content')) { el.scrollTop = el.scrollHeight; }"
)
_REPLY_TEXT_PRESENT_JS: Final[str] = f"""(text) => {{
    {_SCROLL_CHAT_TO_TAIL_JS}
    for (const el of document.querySelectorAll('p, div, span, li, pre, code')) {{
        if (el.textContent !== null && el.textContent.trim() === text) return true;
    }}
    return false;
}}"""
_PROMPT_SHOWN_JS: Final[str] = f"""(marker) => {{
    {_SCROLL_CHAT_TO_TAIL_JS}
    return [...document.querySelectorAll('{USER_TURN_SELECTOR}')].some((el) => (el.textContent ?? '').includes(marker));
}}"""
_SELECT_VALUE_JS: Final[str] = "(select) => select.value"
_ACCOUNT_LABEL_SHOWN_JS: Final[str] = f"""(email) => {{
    const label = document.querySelector('{_CREATE_ACCOUNT_LABEL}');
    return label !== null && label.textContent.trim() === email;
}}"""
_FETCH_JS: Final[str] = """([origin, method, path, body]) => fetch(origin + path, {
        method,
        credentials: 'same-origin',
        headers: body === null ? {} : {'Content-Type': 'application/json'},
        body: body === null ? undefined : JSON.stringify(body),
    }).then((response) => response.text().then((text) => ({status: response.status, body: text})))"""
_SCREENSHOT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")
# What a workspace app's own frontend reads to address its backend (``base-path.ts``
# in the template's workspace-ui): the prefix it is served under, empty at a domain root.
_APP_BASE_PATH_JS: Final[str] = (
    """() => (document.querySelector('meta[name="system-interface-base-path"]')"""
    r"""?.getAttribute('content') ?? '').replace(/\/+$/, '')"""
)


class BackendAnswer(FrozenModel):
    """What one of the app's own routes answered a same-origin fetch."""

    status: int = Field(description="The HTTP status.")
    body: str = Field(description="The response text, JSON for every route the journey reads.")

    def json_body(self) -> Any:
        return json.loads(self.body) if self.body else None


def _fetch_from_page(
    target: Page | Frame, origin: str, method: str, path: str, body: Mapping[str, Any] | None
) -> BackendAnswer:
    """Call ``origin + path`` from a page the app already has open, so that page's session authenticates it."""
    try:
        answer = target.evaluate(_FETCH_JS, [origin, method, path, body])
    except PlaywrightError as exc:
        raise SmokeAppError(f"The page at {target.url} could not fetch {method} {path}: {exc}") from exc
    return BackendAnswer.model_validate(answer)


def fetch_from_app(session: StudioAppSession, method: str, path: str, body: Mapping[str, Any] | None) -> BackendAnswer:
    """Call one of the app's own routes from its chrome page, so its cookie session authenticates the call."""
    return _fetch_from_page(session.page, str(session.backend_origin), method, path, body)


def _fetch_in_chat(chat: Frame, method: str, path: str, body: Mapping[str, Any] | None) -> BackendAnswer:
    """Call one of the workspace chat app's routes from the chat's own page, under the base path it declares.

    An app served behind a path prefix carries it in the meta tag its own
    frontend reads to build every URL; one served at a domain root declares
    none, and the path is used as-is.
    """
    base_path = str(chat.evaluate(_APP_BASE_PATH_JS))
    return _fetch_from_page(chat, base_path, method, path, body)


def fetch_json_from_app(session: StudioAppSession, path: str) -> Any:
    answer = fetch_from_app(session, "GET", path, None)
    if answer.status != 200:
        raise SmokeAppError(f"GET {path} answered {answer.status}: {answer.body[:300]!r}")
    return answer.json_body()


def wait_for_landing_page(session: StudioAppSession) -> str:
    """Wait for the app to show one of its first pages; clear the consent screen if that is the one. Returns the page's selector."""
    page = session.page
    page.wait_for_selector(", ".join(LANDING_SELECTORS), state="attached", timeout=_LANDING_TIMEOUT_MS)
    landed_on = next(selector for selector in LANDING_SELECTORS if page.locator(selector).count() > 0)
    logger.info("The app landed on {}", landed_on)
    if landed_on == _CONSENT_CONTINUE:
        page.click(_CONSENT_CONTINUE)
        page.wait_for_selector(_CONSENT_CONTINUE, state="detached", timeout=_PAGE_TIMEOUT_MS)
        remaining = [selector for selector in LANDING_SELECTORS if selector != _CONSENT_CONTINUE]
        page.wait_for_selector(", ".join(remaining), state="attached", timeout=_LANDING_TIMEOUT_MS)
        landed_on = next(selector for selector in remaining if page.locator(selector).count() > 0)
        logger.info("The consent screen is answered; the app shows {}", landed_on)
    return landed_on


def start_web_login(session: StudioAppSession, landed_on: str) -> str:
    """Open the app's web-login flow the way the page it landed on offers it; return the hosted sign-in URL.

    A fresh install is in its start flow, whose title bar offers "Already set up? Log in"
    throughout; an install past onboarding signs in from Manage Accounts.
    """
    if landed_on == _START_FLOW:
        return _start_web_login_from_start_flow(session)
    return _start_web_login_from_accounts_page(session)


def _start_web_login_from_start_flow(session: StudioAppSession) -> str:
    """Decline error reporting (a test run is not a user's install), then take the existing-login way out."""
    page = session.page
    page.wait_for_selector(_REPORTING_DECLINE, state="visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_REPORTING_DECLINE)
    page.wait_for_selector(_EXISTING_LOGIN_BUTTON, state="visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_EXISTING_LOGIN_BUTTON)
    return _wait_for_web_login_url(page)


def _start_web_login_from_accounts_page(session: StudioAppSession) -> str:
    """Press Add account on Manage Accounts."""
    page = session.page
    page.goto(f"{session.backend_origin}{_ACCOUNTS_PATH}", wait_until="domcontentloaded")
    page.wait_for_selector(_ADD_ACCOUNT_BUTTON, state="visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_ADD_ACCOUNT_BUTTON)
    return _wait_for_web_login_url(page)


def _wait_for_web_login_url(page: Page) -> str:
    """The hosted sign-in URL the web-login modal offers to copy, once the app's loopback listener is up.

    The modal shows the URL once the flow's subprocess has bound its listener,
    or an error with Try again when the subprocess failed to start; a user
    presses Try again, so the run does too, a bounded number of times.
    """
    for attempt in range(1, _WEB_LOGIN_START_ATTEMPTS + 1):
        page.wait_for_selector(
            f"{_WEB_LOGIN_URL}, {_WEB_LOGIN_RETRY}", state="attached", timeout=_WEB_LOGIN_URL_TIMEOUT_MS
        )
        if page.locator(_WEB_LOGIN_URL).count() > 0:
            login_url = page.locator(_WEB_LOGIN_URL).inner_text().strip()
            logger.info("The app's web-login flow is waiting at {}", login_url)
            return login_url
        logger.warning(
            "The app could not start its web-login flow (attempt {} of {}); pressing Try again",
            attempt,
            _WEB_LOGIN_START_ATTEMPTS,
        )
        page.click(_WEB_LOGIN_RETRY)
    raise SmokeAppError(f"The app could not start its web-login flow in {_WEB_LOGIN_START_ATTEMPTS} attempts.")


def _signed_in_account_id(session: StudioAppSession, email: str) -> str | None:
    """The user id the app lists for ``email``, or None while it is not signed in."""
    for account in fetch_json_from_app(session, _ACCOUNTS_API_PATH).get("accounts", []):
        if account.get("email") == email:
            return str(account["account_id"])
    return None


def _account_create_path(account_id: str) -> str:
    return f"/account/{account_id}/create"


def wait_for_account_in_create_form(session: StudioAppSession, email: str, timeout_seconds: float) -> str:
    """Wait for ``email`` to be signed in and for its hub's create form to create under it; return its user id.

    A window creates under the account its hub route names, so the sign-in that
    just landed is checked on that account's create page.
    """
    page = session.page
    deadline = time.monotonic() + timeout_seconds
    account_id = _signed_in_account_id(session, email)
    while account_id is None:
        if time.monotonic() >= deadline:
            raise SmokeAppError(f"The app does not list {email} as a signed-in account after {timeout_seconds}s.")
        threading.Event().wait(timeout=_POLL_SECONDS)
        account_id = _signed_in_account_id(session, email)
    page.goto(f"{session.backend_origin}{_account_create_path(account_id)}", wait_until="domcontentloaded")
    page.wait_for_selector(_CREATE_FORM, state="attached", timeout=_PAGE_TIMEOUT_MS)
    page.wait_for_function(_ACCOUNT_LABEL_SHOWN_JS, arg=email, timeout=max(deadline - time.monotonic(), 1.0) * 1000)
    logger.info("The create form on {}'s hub creates under that account", email)
    return account_id


def create_imbue_cloud_workspace(session: StudioAppSession, workspace_name: str, account_email: str) -> Frame:
    """Drive the account's create form to an Imbue Cloud workspace; return its rendered desktop's frame."""
    page = session.page
    account_id = _signed_in_account_id(session, account_email)
    if account_id is None:
        raise SmokeAppError(f"The app does not list {account_email} as a signed-in account.")
    page.goto(f"{session.backend_origin}{_account_create_path(account_id)}", wait_until="domcontentloaded")
    page.wait_for_selector(_CREATE_FORM, state="attached", timeout=_PAGE_TIMEOUT_MS)
    page.wait_for_function(_ACCOUNT_LABEL_SHOWN_JS, arg=account_email, timeout=_PAGE_TIMEOUT_MS)
    page.click(_REMOTE_PRESET)
    page.wait_for_selector(f"{_ADVANCED_TOGGLE}:visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_ADVANCED_TOGGLE)
    page.wait_for_selector(f"{_HOST_NAME_FIELD}:visible", timeout=_PAGE_TIMEOUT_MS)
    page.fill(_HOST_NAME_FIELD, workspace_name)
    page.select_option(_LAUNCH_MODE_SELECT, IMBUE_CLOUD_LAUNCH_MODE)
    page.wait_for_selector(f"{_REGION_SELECT}:visible", timeout=_PAGE_TIMEOUT_MS)
    # The region the form already points at, which is the one the app picks for
    # where this machine is.
    region = str(page.eval_on_selector(_REGION_SELECT, _SELECT_VALUE_JS))
    if not region:
        raise SmokeAppError("The create form offers no Imbue Cloud region to create the workspace in.")
    # The form shares a workspace on an account as it is created; the journey
    # shares it from the Share tab instead, which only publishes an unshared one.
    _turn_switch_off(page, _CREATE_SHARING_SWITCH)
    logger.info("Creating Imbue Cloud workspace {!r} as {} in region {}", workspace_name, account_email, region)
    page.click(_CREATE_SUBMIT)
    page.wait_for_selector(_CREATING_PAGE, state="attached", timeout=_PAGE_TIMEOUT_MS)
    workspace_frame = wait_for_workspace_ready_or_failure(session.browser, page, _CREATE_TIMEOUT_SECONDS)
    workspace_frame.wait_for_selector(DESKTOP_BACKDROP_SELECTOR, state="visible", timeout=_DESKTOP_TIMEOUT_MS)
    logger.info("The workspace's desktop rendered at {}", workspace_frame.url)
    return workspace_frame


def _turn_switch_off(page: Page, switch: str) -> None:
    """Flip a ``role="switch"`` control off unless it already is, and wait until it says it is off."""
    if page.get_attribute(switch, "aria-checked") == "true":
        page.click(switch)
    page.wait_for_selector(f'{switch}[aria-checked="false"]', state="visible", timeout=_PAGE_TIMEOUT_MS)


def agent_id_of_workspace_frame(session: StudioAppSession, workspace_frame: Frame) -> str:
    """The workspace's agent id, read from its frame's origin (translated through the listing for a host-keyed one)."""
    coordinate = workspace_coordinate_from_subdomain(workspace_frame.url)
    if coordinate.startswith("agent-"):
        return coordinate
    for row in list_workspaces(session):
        if row.get("host_id") == coordinate and row.get("agent_id"):
            return str(row["agent_id"])
    raise SmokeAppError(f"No workspace with host id {coordinate!r} in {_WORKSPACES_PATH}")


def list_workspaces(session: StudioAppSession) -> list[dict[str, Any]]:
    listing = fetch_json_from_app(session, _WORKSPACES_PATH)
    return [row for row in listing.get("workspaces", []) if isinstance(row, dict)]


def open_welcome_chat(workspace_frame: Frame) -> Frame:
    """The chat a fresh workspace opens on: the conversation its creation page seeded.

    Returned before any composer renders, which is what it is for here: on a
    workspace with no AI provider yet, the composer is replaced by the
    "Connect an AI provider" notice, and this page is the journey's way onto
    the chat app's origin while that is so.
    """
    chat = chat_frame(workspace_frame, _CHAT_FRAME_TIMEOUT_SECONDS)
    logger.info("The workspace's welcome chat is framed at {}", chat.url)
    return chat


def mint_workspace_llm_credentials(session: StudioAppSession, agent_id: str) -> str:
    """Mint the account's LiteLLM key for the workspace through the desktop, as the AI-keys modal does."""
    answer = fetch_from_app(session, "POST", _MINT_AI_KEY_PATH, {"workspace": agent_id})
    if answer.status != 200:
        raise SmokeAppError(f"POST {_MINT_AI_KEY_PATH} answered {answer.status}: {answer.body[:300]!r}")
    body = answer.json_body()
    credentials = body.get("credentials") if isinstance(body, dict) else None
    if not isinstance(credentials, str) or not credentials.strip():
        raise SmokeAppError(f"POST {_MINT_AI_KEY_PATH} answered no credentials: {answer.body[:300]!r}")
    logger.info("Minted the workspace's LLM credentials through the desktop")
    return credentials


def adopt_imbue_credentials(chat: Frame, credential_blob: str) -> str:
    """Give the workspace the minted credentials as a provider account; return the account it made.

    ``/api/claude-auth/submit-credentials`` is the chat app's endpoint for
    exactly this blob -- the cross-repo contract the desktop's AI-keys page
    mints for -- and it answers with the auth mode it understood, so a key
    that was read as a plain Anthropic key (routing off the Imbue proxy
    rather than through it) fails here rather than at the first reply. The
    chooser's own lane, whose paste textarea takes this blob, is not driven
    yet; that is a follow-up recorded in the spec.
    """
    answer = _fetch_in_chat(chat, "POST", _SUBMIT_CREDENTIALS_PATH, {"credentials": credential_blob})
    if answer.status != 200:
        raise SmokeAppError(f"POST {_SUBMIT_CREDENTIALS_PATH} answered {answer.status}: {answer.body[:300]!r}")
    body = answer.json_body()
    if not isinstance(body, dict) or body.get("auth_mode") != _IMBUE_AUTH_MODE:
        raise SmokeAppError(
            f"The workspace read the minted credentials as {str(body)[:300]!r}, "
            f"not as the proxied {_IMBUE_AUTH_MODE!r} setup they are."
        )
    account_id = str(body.get("account_id", ""))
    logger.info("The workspace adopted the minted credentials as account {}", account_id)
    return account_id


def send_prompt_to_main_chat(workspace_frame: Frame, welcome_chat: Frame, prompt: str, marker: str) -> str:
    """Send ``prompt`` with the desktop launcher's Enter, which sends a text to the workspace's main chat; return that
    chat's id once its page shows the prompt (``marker`` is a string only the prompt carries).

    A fresh workspace's main chat is its welcome chat, still awaiting its first send: the prompt is that send, and it
    launches the chat's agent on the account signed in by then, so the credentials are adopted before this runs.
    """
    main_chat_id = _main_chat_id(welcome_chat, _MAIN_CHAT_TIMEOUT_SECONDS)
    send_to_main_chat_from_launcher(workspace_frame, prompt)
    _wait_until_chat_shows(
        workspace_frame, main_chat_id, _PROMPT_SHOWN_JS, marker, _PROMPT_SHOWN_TIMEOUT_SECONDS, "the prompt"
    )
    logger.info("Confirmed that the main chat {} shows the prompt", main_chat_id)
    return main_chat_id


def _main_chat_id(chat: Frame, timeout_seconds: float) -> str:
    """The workspace's main chat, as the chat app's listing names it."""
    deadline = time.monotonic() + timeout_seconds
    last_outcome = "not asked"
    while time.monotonic() < deadline:
        answer = _fetch_in_chat(chat, "GET", _CHATS_PATH, None)
        body = answer.json_body() if answer.status == 200 else None
        main_chat_id = body.get("main_chat_id") if isinstance(body, dict) else None
        if isinstance(main_chat_id, str) and main_chat_id:
            return main_chat_id
        last_outcome = f"status {answer.status}: {answer.body[:300]!r}"
        threading.Event().wait(timeout=_POLL_SECONDS)
    raise SmokeAppError(
        f"GET {_CHATS_PATH} named no main chat within {timeout_seconds:.0f}s; last answer {last_outcome}"
    )


def wait_for_reply(workspace_frame: Frame, chat_id: str, text: str, timeout_seconds: float) -> float:
    """Wait for chat ``chat_id`` to show a message whose whole text is ``text``; return how long it took."""
    return _wait_until_chat_shows(workspace_frame, chat_id, _REPLY_TEXT_PRESENT_JS, text, timeout_seconds, repr(text))


def _wait_until_chat_shows(
    workspace_frame: Frame, chat_id: str, predicate_js: str, arg: str, timeout_seconds: float, what: str
) -> float:
    """Poll the page of chat ``chat_id`` until ``predicate_js`` holds for ``arg``; return how long it took.

    The page is looked up again on every poll: a text arriving through the intake navigates the window showing the
    chat, so its frame can be a new one.
    """
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    last_outcome = "no page shows the chat"
    while time.monotonic() < deadline:
        chat = find_chat_frame(workspace_frame, chat_id)
        if chat is not None:
            try:
                if chat.evaluate(predicate_js, arg):
                    return time.monotonic() - started_at
                last_outcome = f"its page at {chat.url} does not show it yet"
            except PlaywrightError as exc:
                last_outcome = f"its page at {chat.url} could not be read: {exc}"
        threading.Event().wait(timeout=_POLL_SECONDS)
    raise SmokeAppError(f"Chat {chat_id} did not show {what} within {timeout_seconds:.0f}s; {last_outcome}")


def publish_share_from_panel(session: StudioAppSession, agent_id: str) -> None:
    """Turn sharing on from the Share tab and wait until the link is live (or publishing is refused)."""
    page = session.page
    page.goto(f"{session.backend_origin}{_SHARE_TAB_PATH.format(agent_id=agent_id)}", wait_until="domcontentloaded")
    page.wait_for_selector(_PUBLISH_SWITCH, state="visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_PUBLISH_SWITCH)
    page.wait_for_selector(_PUBLISH_CONFIRM, state="visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_PUBLISH_CONFIRM)
    page.wait_for_selector(_SHARE_LINK, state="attached", timeout=_PAGE_TIMEOUT_MS)
    page.wait_for_selector(f"{_SHARE_COPY}, {_PUBLISH_ERROR}", state="visible", timeout=_PUBLISH_TIMEOUT_MS)
    refusal = page.locator(_PUBLISH_ERROR)
    if refusal.count() > 0:
        raise SmokeAppError(f"The workspace could not be published: {refusal.first.inner_text()}")
    logger.info("Workspace {} is published; the Share tab shows its link", agent_id)


def unpublish_share_from_panel(session: StudioAppSession, agent_id: str) -> None:
    """Turn sharing off from the Share tab and wait for the panel to show it off.

    The switch pointing off, with its link section gone, is what the panel
    shows every workspace whose sharing was turned off; the "sharing is off"
    notice beside it is only drawn for a workspace that also has grants, which
    a journey that shares with nobody never has.
    """
    page = session.page
    page.goto(f"{session.backend_origin}{_SHARE_TAB_PATH.format(agent_id=agent_id)}", wait_until="domcontentloaded")
    page.wait_for_selector(_PUBLISH_SWITCH_ON, state="visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_PUBLISH_SWITCH)
    page.wait_for_selector(_PUBLISH_SWITCH_SETTLED_OFF, state="visible", timeout=_PUBLISH_TIMEOUT_MS)
    page.wait_for_selector(_SHARE_LINK, state="detached", timeout=_PAGE_TIMEOUT_MS)
    logger.info("Workspace {} is no longer published", agent_id)


def read_sharing_status(session: StudioAppSession, agent_id: str) -> dict[str, Any]:
    status = fetch_json_from_app(session, _SHARING_PATH.format(agent_id=agent_id))
    if not isinstance(status, dict):
        raise SmokeAppError(f"The sharing status of {agent_id} is not an object: {str(status)[:200]}")
    return status


@pure
def share_url_for_service(status: Mapping[str, Any], service: str) -> str | None:
    """The link to one share target, or None until the share has a URL and the target a label.

    The status carries the share's base URL (whose bare domain does not route)
    and a label per registered service; the link that routes is
    ``https://<label>.<domain>/``, the way the Share tab builds it.
    """
    labels = status.get("service_labels")
    label = labels.get(service) if isinstance(labels, Mapping) else None
    host = urlsplit(str(status.get("url") or "")).hostname
    if not label or not host:
        return None
    return f"https://{label}.{host}/"


def wait_for_share_url(session: StudioAppSession, agent_id: str, service: str, timeout_seconds: float) -> str:
    """Wait for the share to be live end to end and for ``service`` to have a link; return the link."""
    deadline = time.monotonic() + timeout_seconds
    last_status: dict[str, Any] = {}
    last_readiness: Any = None
    while time.monotonic() < deadline:
        last_status = read_sharing_status(session, agent_id)
        last_readiness = fetch_json_from_app(session, _SHARING_READINESS_PATH.format(agent_id=agent_id))
        url = share_url_for_service(last_status, service)
        if url is not None and isinstance(last_readiness, dict) and last_readiness.get("ready"):
            return url
        threading.Event().wait(timeout=_POLL_SECONDS)
    raise SmokeAppError(
        f"The share of {agent_id} did not offer a live link for {service!r} within {timeout_seconds:.0f}s; "
        f"last status: enabled={last_status.get('enabled')}, url={last_status.get('url')}, "
        f"service_labels={last_status.get('service_labels')}; last readiness: {last_readiness}"
    )


def destroy_workspace_from_settings(session: StudioAppSession, agent_id: str) -> None:
    """Press Destroy, then confirm, on the workspace's Settings tab."""
    page = session.page
    page.goto(f"{session.backend_origin}{_SETTINGS_TAB_PATH.format(agent_id=agent_id)}", wait_until="domcontentloaded")
    page.wait_for_selector(_DESTROY_BUTTON, state="visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_DESTROY_BUTTON)
    page.wait_for_selector(_DESTROY_CONFIRM, state="visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_DESTROY_CONFIRM)
    logger.info("Confirmed the destroy of workspace {}", agent_id)


def wait_for_destroy_done(session: StudioAppSession, agent_id: str, timeout_seconds: float) -> None:
    """Wait until the app stops listing the workspace as a live host.

    The listing is the terminal signal rather than the destroy operation's own
    status: that record answers 404 both before the destroy has registered and
    after its record is reclaimed, so polling it alone reads "already gone" the
    moment the confirm is pressed. Its last word rides along in the failure
    message, where it says how far the destroy got.
    """
    deadline = time.monotonic() + timeout_seconds
    last_answer: BackendAnswer | None = None
    while time.monotonic() < deadline:
        # The chrome page routes away from the settings tab once the destroy is
        # confirmed, which can tear down the document a fetch was evaluated in.
        try:
            if is_workspace_gone(list_workspaces(session), agent_id):
                logger.info("The destroy of workspace {} is done", agent_id)
                return
            last_answer = fetch_from_app(session, "GET", _DESTROY_OPERATION_PATH.format(agent_id=agent_id), None)
        except SmokeAppError as exc:
            logger.debug("The destroy poll could not run while the page navigated: {}", exc)
        threading.Event().wait(timeout=_POLL_SECONDS)
    raise SmokeAppError(
        f"Workspace {agent_id} was still listed {timeout_seconds:.0f}s after its destroy was confirmed; "
        f"its destroy operation last answered {last_answer}"
    )


@pure
def is_workspace_gone(listing: list[dict[str, Any]], agent_id: str) -> bool:
    """Whether the listing no longer shows ``agent_id`` as a workspace with a host.

    A destroyed workspace with backups stays listed with no host state, which
    counts as gone: nothing is leased or running for it any more.
    """
    for row in listing:
        if row.get("agent_id") != agent_id:
            continue
        host_state = row.get("host_state")
        if host_state is not None and host_state != "DESTROYED":
            return False
    return True


def screenshot_every_page(browser: Browser, output_dir: Path, label: str) -> list[Path]:
    """Save a screenshot of every page the app has open, named by ``label``; never raises."""
    saved: list[Path] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_label = _SCREENSHOT_NAME_PATTERN.sub("-", label).strip("-") or "page"
    pages = [page for context in browser.contexts for page in context.pages]
    for page_idx, page in enumerate(pages):
        path = output_dir / f"{safe_label}-{page_idx}.png"
        try:
            page.screenshot(path=str(path), full_page=False)
        except (PlaywrightError, OSError) as exc:
            logger.warning("Could not screenshot page {} ({}): {}", page_idx, page.url, exc)
            continue
        saved.append(path)
    logger.info("Saved {} screenshot(s) for {} under {}", len(saved), label, output_dir)
    return saved
