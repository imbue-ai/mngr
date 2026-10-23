"""The journey's steps inside the launched Imbue Studio app, driven through its own UI.

Every control here is the one a user presses: the landing page and its
consent screen, the Manage Accounts page's sign-in, the create form, the
launcher's new chat and its composer, the Share tab, and the Settings tab's
destroy. What the UI did is then confirmed by reading the app's own routes,
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
from imbue.minds.desktop_client.e2e_workspace_runner import CHAT_INPUT_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import CHAT_PAGE_URL_PATTERN
from imbue.minds.desktop_client.e2e_workspace_runner import DESKTOP_BACKDROP_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import USER_TURN_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import chat_frame
from imbue.minds.desktop_client.e2e_workspace_runner import start_new_chat_from_launcher
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
# ``data-answer``; the first question's quiet way out, for someone who already
# has an account, is the row's aside.
_REPORTING_DECLINE: Final[str] = '[data-answer="reporting-no"]'
_EXISTING_LOGIN_ASIDE: Final[str] = "button[data-aside]"
# The Manage Accounts page's one button, and the web-login modal's copy of the sign-in URL.
_ACCOUNTS_PATH: Final[str] = "/accounts"
_ADD_ACCOUNT_BUTTON: Final[str] = 'button:has-text("Add account")'
_WEB_LOGIN_URL: Final[str] = "#web-login-url"
# The modal's Try again, shown when the app could not start its sign-in flow.
_WEB_LOGIN_RETRY: Final[str] = "#web-login-retry-btn"
# A user presses Try again when the flow fails to start; the smoke run does too.
_WEB_LOGIN_START_ATTEMPTS: Final[int] = 3
# The create form, plus the compute preset cards.
_CREATE_PATH: Final[str] = "/create"
_CREATE_FORM: Final[str] = "#create-form"
_REMOTE_PRESET: Final[str] = '[data-preset="remote"]'
_ADVANCED_TOGGLE: Final[str] = "#toggle-advanced"
_HOST_NAME_FIELD: Final[str] = "#host_name"
_ACCOUNT_SELECT: Final[str] = "#account_id"
_LAUNCH_MODE_SELECT: Final[str] = "#launch_mode"
_REGION_SELECT: Final[str] = "#region"
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
# Its listing of the chats it knows, which is what a chat has to be in before it can be sent to.
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
# A chat minted from the launcher is created asynchronously before its composer shows.
_CHAT_INPUT_TIMEOUT_MS: Final[float] = 240_000.0
# Its agent is started after that again, and only then is the chat something to send to.
_CHAT_LISTED_TIMEOUT_SECONDS: Final[float] = 300.0
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
_SELECT_VALUE_JS: Final[str] = "(select) => select.value"
_ACCOUNT_OPTION_PRESENT_JS: Final[str] = f"""(email) => [...document.querySelectorAll('{_ACCOUNT_SELECT} option')]
    .some((option) => option.textContent.trim() === email)"""
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

    A fresh install is in its start flow, whose first question has a quiet
    "I already have a workspace (log in)" way out; an install past onboarding
    signs in from Manage Accounts.
    """
    if landed_on == _START_FLOW:
        return _start_web_login_from_start_flow(session)
    return _start_web_login_from_accounts_page(session)


def _start_web_login_from_start_flow(session: StudioAppSession) -> str:
    """Decline error reporting (a test run is not a user's install), then take the existing-login way out."""
    page = session.page
    page.wait_for_selector(_REPORTING_DECLINE, state="visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_REPORTING_DECLINE)
    page.wait_for_selector(_EXISTING_LOGIN_ASIDE, state="visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_EXISTING_LOGIN_ASIDE)
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


def wait_for_account_in_create_form(session: StudioAppSession, email: str, timeout_seconds: float) -> None:
    """Wait for the create form to list ``email`` among the accounts a workspace can run on."""
    page = session.page
    page.goto(f"{session.backend_origin}{_CREATE_PATH}", wait_until="domcontentloaded")
    page.wait_for_selector(_CREATE_FORM, state="attached", timeout=_PAGE_TIMEOUT_MS)
    page.wait_for_function(_ACCOUNT_OPTION_PRESENT_JS, arg=email, timeout=timeout_seconds * 1000)
    logger.info("The create form lists {} as an account", email)


def create_imbue_cloud_workspace(session: StudioAppSession, workspace_name: str, account_email: str) -> Frame:
    """Drive the create form to an Imbue Cloud workspace on the account; return its rendered desktop's frame."""
    page = session.page
    page.goto(f"{session.backend_origin}{_CREATE_PATH}", wait_until="domcontentloaded")
    page.wait_for_selector(_CREATE_FORM, state="attached", timeout=_PAGE_TIMEOUT_MS)
    page.click(_REMOTE_PRESET)
    page.wait_for_selector(f"{_ADVANCED_TOGGLE}:visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_ADVANCED_TOGGLE)
    page.wait_for_selector(f"{_HOST_NAME_FIELD}:visible", timeout=_PAGE_TIMEOUT_MS)
    page.fill(_HOST_NAME_FIELD, workspace_name)
    page.select_option(_ACCOUNT_SELECT, label=account_email)
    page.select_option(_LAUNCH_MODE_SELECT, IMBUE_CLOUD_LAUNCH_MODE)
    page.wait_for_selector(f"{_REGION_SELECT}:visible", timeout=_PAGE_TIMEOUT_MS)
    # The region the form already points at, which is the one the app picks for
    # where this machine is.
    region = str(page.eval_on_selector(_REGION_SELECT, _SELECT_VALUE_JS))
    if not region:
        raise SmokeAppError("The create form offers no Imbue Cloud region to create the workspace in.")
    logger.info("Creating Imbue Cloud workspace {!r} as {} in region {}", workspace_name, account_email, region)
    page.click(_CREATE_SUBMIT)
    page.wait_for_selector(_CREATING_PAGE, state="attached", timeout=_PAGE_TIMEOUT_MS)
    workspace_frame = wait_for_workspace_ready_or_failure(session.browser, page, _CREATE_TIMEOUT_SECONDS)
    workspace_frame.wait_for_selector(DESKTOP_BACKDROP_SELECTOR, state="visible", timeout=_DESKTOP_TIMEOUT_MS)
    logger.info("The workspace's desktop rendered at {}", workspace_frame.url)
    return workspace_frame


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


def open_new_chat(workspace_frame: Frame) -> Frame:
    """Start a chat from the desktop's launcher and return its frame once its composer is ready.

    The welcome chat cannot take the prompt: a workspace that booted without
    AI credentials renders the no-provider notice in place of its composer,
    and the page reads its accounts only as it boots, so the composer appears
    only after a reload -- which the welcome chat does not survive, since its
    chat is created by its own first send and a reloaded page sends to a chat
    the workspace does not have yet ("Chat '<id>' not found").

    A chat started from the launcher is minted by the chat app at that
    moment, on the account signed in by then: the launcher re-reads the
    accounts before choosing one, so the credentials adopted out of band are
    the account it starts on.
    """
    chat = start_new_chat_from_launcher(workspace_frame)
    chat.wait_for_selector(CHAT_INPUT_SELECTOR, state="visible", timeout=_CHAT_INPUT_TIMEOUT_MS)
    _wait_until_chat_is_listed(chat, _CHAT_LISTED_TIMEOUT_SECONDS)
    logger.info("A new chat is open at {}", chat.url)
    return chat


def _wait_until_chat_is_listed(chat: Frame, timeout_seconds: float) -> None:
    """Wait until the chat app lists ``chat``, which is when the chat is something to send to.

    A create answers as soon as the chat has an id, and its composer renders
    at once, but its agent is still starting; the chat app lists a chat only
    once mngr has registered that agent, and a send before then is refused
    with "Chat '<id>' not found".
    """
    match = CHAT_PAGE_URL_PATTERN.search(chat.url.split("?", 1)[0])
    if match is None:
        raise SmokeAppError(f"The chat page at {chat.url} does not name a chat in its URL.")
    chat_id = match.group(1)
    deadline = time.monotonic() + timeout_seconds
    last_outcome = "not asked"
    while time.monotonic() < deadline:
        answer = _fetch_in_chat(chat, "GET", _CHATS_PATH, None)
        body = answer.json_body() if answer.status == 200 else None
        rows = body.get("chats", []) if isinstance(body, dict) else []
        listed = [str(row.get("chat_id")) for row in rows if isinstance(row, dict)]
        if chat_id in listed:
            return
        last_outcome = (
            f"status {answer.status}, listed {listed}" if answer.status == 200 else f"status {answer.status}"
        )
        threading.Event().wait(timeout=_POLL_SECONDS)
    raise SmokeAppError(
        f"The chat app did not list chat {chat_id} within {timeout_seconds:.0f}s; last answer: {last_outcome}"
    )


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


def send_prompt(chat: Frame, prompt: str) -> None:
    """Type ``prompt`` into the chat's composer and send it; the user turn must render."""
    chat.fill(CHAT_INPUT_SELECTOR, prompt)
    chat.press(CHAT_INPUT_SELECTOR, "Enter")
    chat.wait_for_selector(USER_TURN_SELECTOR, state="attached", timeout=_PAGE_TIMEOUT_MS)


def wait_for_reply(chat: Frame, text: str, timeout_seconds: float) -> float:
    """Wait for the chat to show a message whose whole text is ``text``; return how long it took."""
    started_at = time.monotonic()
    chat.wait_for_function(_REPLY_TEXT_PRESENT_JS, arg=text, timeout=timeout_seconds * 1000)
    return time.monotonic() - started_at


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
