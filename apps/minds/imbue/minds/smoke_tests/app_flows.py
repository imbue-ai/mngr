"""The journey's steps inside the launched Imbue Studio app, driven through its own UI.

Every control here is the one a user presses on a fresh install: the first-run
page's error-reporting checkbox and its Sign in, the Next on the workspace's
provider page, the desktop's launcher field, the home page's Stop and Start,
Settings' Sharing page, and its General page's destroy.
What the UI did is then confirmed by reading the app's own routes,
through a same-origin fetch on a page it already has open -- the chrome page
for the desktop's routes, a page of the chat app's for the workspace's -- so
each one's session authenticates the call.

Two routes are called rather than driven through the provider page's tiles:
the desktop's AI-key mint, and the workspace's adoption of the blob it mints.
They are the routes behind the chooser's "Sign in with Imbue" lane; driving
that lane's own controls is a follow-up recorded in the spec.
"""

import re
import threading
import time
from collections.abc import Mapping
from http import HTTPMethod
from pathlib import Path
from typing import Any
from typing import Final
from urllib.parse import urlsplit

from loguru import logger
from playwright.sync_api import Browser
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame
from playwright.sync_api import Locator
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.e2e_workspace_runner import FIRST_RUN_ACCOUNT_SELECTOR_TEMPLATE
from imbue.minds.desktop_client.e2e_workspace_runner import FIRST_RUN_PROVIDERS_TIMEOUT_SECONDS
from imbue.minds.desktop_client.e2e_workspace_runner import PageFetchAnswer
from imbue.minds.desktop_client.e2e_workspace_runner import USER_TURN_SELECTOR
from imbue.minds.desktop_client.e2e_workspace_runner import chat_frame
from imbue.minds.desktop_client.e2e_workspace_runner import fetch_from_page
from imbue.minds.desktop_client.e2e_workspace_runner import find_chat_frame
from imbue.minds.desktop_client.e2e_workspace_runner import first_run_providers_frame
from imbue.minds.desktop_client.e2e_workspace_runner import open_account_home_page
from imbue.minds.desktop_client.e2e_workspace_runner import request_chat_api
from imbue.minds.desktop_client.e2e_workspace_runner import send_to_main_chat_from_launcher
from imbue.minds.desktop_client.e2e_workspace_runner import stop_workspace_from_home_page
from imbue.minds.desktop_client.e2e_workspace_runner import wait_for_workspace_ready_or_failure
from imbue.minds.desktop_client.e2e_workspace_runner import wait_out_first_run_cover
from imbue.minds.desktop_client.e2e_workspace_runner import workspace_coordinate_from_subdomain
from imbue.minds.smoke_tests.data_types import StudioAppSession
from imbue.minds.smoke_tests.primitives import SmokeAppError

# The pages an app can open on: the first-run sign-in page of a fresh install, the consent screen that can
# precede it, the home page of an install with workspaces, or a workspace.
_LANDING_SELECTORS: Final[tuple[str, ...]] = (
    "#start-flow",
    "#consent-continue",
    "#landing-minds-settings",
    "#content-frame",
)
_CONSENT_CONTINUE: Final[str] = "#consent-continue"
_SIGN_IN_PAGE: Final[str] = "#start-flow"
_HOME_PAGE: Final[str] = "#landing-minds-settings"
# The first-run sign-in page (``StartPage.ts``): its error-reporting checkbox, which starts checked, its
# Sign in, which records the checkbox and opens the web-login modal, and what it says when the create it
# submits for the account that landed is refused.
_REPORTING_CHECKBOX: Final[str] = "#start-reporting-checkbox"
_SIGN_IN_BUTTON: Final[str] = "#start-sign-in"
_CREATE_REFUSED: Final[str] = "#start-refused"
_SIGN_IN_STAGE_JS: Final[str] = "() => document.querySelector('#start-flow')?.getAttribute('data-stage') ?? null"
_CONSENT_API_PATH: Final[str] = "/ui/api/onboarding/consent"
# The web-login modal's copy of the sign-in URL.
_WEB_LOGIN_URL: Final[str] = "#web-login-url"
# The modal's Try again, shown when the app could not start its sign-in flow.
_WEB_LOGIN_RETRY: Final[str] = "#web-login-retry-btn"
# A user presses Try again when the flow fails to start; the smoke run does too.
_WEB_LOGIN_START_ATTEMPTS: Final[int] = 3
_ACCOUNTS_API_PATH: Final[str] = "/api/v1/accounts"
# The creation page's heading (``FirstRunWelcome.ts``), which welcomes the owner of a first workspace.
_CREATING_HEADING: Final[str] = "#creating-welcome"
# Settings' Sharing page for the whole workspace (``ShareTab.ts``) and its General page's destroy
# (``SettingsGroups.ts``).
_SHARE_TAB_PATH: Final[str] = "/settings?workspace={agent_id}&tab=workspace&group=sharing"
_SETTINGS_TAB_PATH: Final[str] = "/settings?workspace={agent_id}&tab=workspace&group=general"
_PUBLISH_SWITCH: Final[str] = "#ws-share-publish-switch"
# The switch is a ``role="switch"`` button: it says which way it points, and says
# it is busy while the publish or unpublish it started is still settling.
_PUBLISH_SWITCH_ON: Final[str] = f'{_PUBLISH_SWITCH}[aria-checked="true"]'
_PUBLISH_SWITCH_SETTLED_OFF: Final[str] = f'{_PUBLISH_SWITCH}[aria-checked="false"]:not([aria-busy])'
# The page draws the switch only once Imbue Cloud has confirmed what a first, local read said, and the
# switch is busy through a fresh read too: on and at rest, the page has settled on a shared workspace.
_PUBLISH_SWITCH_SETTLED_ON: Final[str] = f"{_PUBLISH_SWITCH_ON}:not([aria-busy])"
_SHARE_LINK: Final[str] = "#ws-share-link"
# The notices the page draws when something stands in the way of a grant or its invitation.
_SHARE_NOTICES: Final[tuple[str, ...]] = (
    "#ws-share-off-notice",
    "#ws-share-load-error",
    "#ws-share-publish-error",
    "#ws-share-sync-notice",
)
# The add row. Its controls are marked off until the page knows the workspace is shared and has read who
# it admits, and pressing Add does nothing until then.
_GRANT_ADD_VALUE: Final[str] = "#ws-share-add-value"
_GRANT_ADD_BUTTON: Final[str] = "#ws-share-add-btn"
_GRANT_ADD_BUTTON_ONCE_SETTLED: Final[str] = (
    f"body:has({_PUBLISH_SWITCH_SETTLED_ON}) {_GRANT_ADD_BUTTON}:not([aria-disabled])"
)
_GRANT_ADD_REFUSAL: Final[str] = "#ws-share-refusal"
# A grant's card in the list of who has access. A failed save is marked on it, with its sentence in the
# mark's tooltip. The card's invitation slot reads "Inviting" while the invitation is out; an invitation
# that was refused gets a line of its own instead.
_GRANT_CARD: Final[str] = "#ws-share-grants [data-grant-row]"
_GRANT_SAVE_FAILURE: Final[str] = "[data-grant-failure]"
_TOOLTIP_ATTRIBUTE: Final[str] = "data-tooltip"
_GRANT_INVITATION_ANSWER: Final[str] = '[data-slot="invitation"]:not(:has-text("Inviting")), [data-invite-refusal]'
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
# The sign-in page reads the create form's defaults under the account that landed before it submits.
_FIRST_CREATE_SUBMIT_TIMEOUT_MS: Final[float] = 180_000.0
# A pre-baked pool host is adopted in a couple of minutes; when none matches
# the app leases any host and rebuilds the workspace on it, which takes ten
# minutes and more.
_CREATE_TIMEOUT_SECONDS: Final[int] = 1200
# The provider page hears of an account adopted behind it from the chat app's own push.
_ACCOUNT_LISTED_TIMEOUT_MS: Final[float] = 60_000.0
# The chat's page is framed by the desktop's app list, which arrives after the backdrop.
_CHAT_FRAME_TIMEOUT_SECONDS: Final[float] = 240.0
# The chat app answers its listing only once it has read the agent list from mngr.
_MAIN_CHAT_TIMEOUT_SECONDS: Final[float] = 120.0
# The launcher's text reaches the main chat through the chat app's intake, which the chat's page then sends.
_PROMPT_SHOWN_TIMEOUT_SECONDS: Final[float] = 120.0
_UNPUBLISH_TIMEOUT_MS: Final[float] = 300_000.0
# The read that settles the Sharing page asks Imbue Cloud about the share and execs into the workspace.
_SHARE_PAGE_SETTLE_TIMEOUT_MS: Final[float] = 60_000.0
# A grant is written into the workspace and pushed to Imbue Cloud, a second time when its address turns
# out to be an account's, before the invitation is asked for.
_GRANT_TIMEOUT_MS: Final[float] = 120_000.0
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
_SCREENSHOT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")


def fetch_from_app(
    session: StudioAppSession, method: HTTPMethod, path: str, body: Mapping[str, Any] | None
) -> PageFetchAnswer:
    """Call one of the app's own routes from its chrome page, so its cookie session authenticates the call."""
    page = session.page
    try:
        return fetch_from_page(page, str(session.backend_origin), method, path, body)
    except PlaywrightError as exc:
        raise SmokeAppError(f"The page at {page.url} could not fetch {method.value} {path}: {exc}") from exc


def _fetch_in_chat(chat: Frame, method: HTTPMethod, path: str, body: Mapping[str, Any] | None) -> PageFetchAnswer:
    """Call one of the workspace chat app's routes from the chat's own page, under the base path it declares."""
    try:
        return request_chat_api(chat, method, path, body)
    except PlaywrightError as exc:
        raise SmokeAppError(f"The chat page at {chat.url} could not fetch {method.value} {path}: {exc}") from exc


def fetch_json_from_app(session: StudioAppSession, path: str) -> Any:
    answer = fetch_from_app(session, HTTPMethod.GET, path, None)
    if answer.status != 200:
        raise SmokeAppError(f"GET {path} answered {answer.status}: {answer.body[:300]!r}")
    return answer.json_body()


def _wait_for_landing_page(session: StudioAppSession) -> str:
    """Wait for the app to show one of its first pages; clear the consent screen if that is the one. Returns the page's selector."""
    page = session.page
    page.wait_for_selector(", ".join(_LANDING_SELECTORS), state="attached", timeout=_LANDING_TIMEOUT_MS)
    landed_on = next(selector for selector in _LANDING_SELECTORS if page.locator(selector).count() > 0)
    logger.info("The app landed on {}", landed_on)
    if landed_on == _CONSENT_CONTINUE:
        page.click(_CONSENT_CONTINUE)
        page.wait_for_selector(_CONSENT_CONTINUE, state="detached", timeout=_PAGE_TIMEOUT_MS)
        remaining = [selector for selector in _LANDING_SELECTORS if selector != _CONSENT_CONTINUE]
        page.wait_for_selector(", ".join(remaining), state="attached", timeout=_LANDING_TIMEOUT_MS)
        landed_on = next(selector for selector in remaining if page.locator(selector).count() > 0)
        logger.info("The consent screen is answered; the app shows {}", landed_on)
    return landed_on


def wait_for_sign_in_page(session: StudioAppSession) -> None:
    """Wait for the app to open on its first-run sign-in page, which only a fresh install does."""
    landed_on = _wait_for_landing_page(session)
    if landed_on != _SIGN_IN_PAGE:
        raise SmokeAppError(
            f"The journey needs a fresh install: the app opened on {landed_on}, not on its first-run "
            f"sign-in page ({_SIGN_IN_PAGE}), so its data home already holds an install past the first run."
        )


def start_web_login_from_sign_in_page(session: StudioAppSession) -> str:
    """Untick error reporting (a test run is not a user's install) and press Sign in; return the hosted sign-in URL."""
    page = session.page
    page.wait_for_selector(_REPORTING_CHECKBOX, state="visible", timeout=_PAGE_TIMEOUT_MS)
    page.uncheck(_REPORTING_CHECKBOX)
    # The page posts the checkbox's answer as Sign in is pressed, without waiting on it.
    with page.expect_response(
        lambda response: urlsplit(response.url).path == _CONSENT_API_PATH, timeout=_PAGE_TIMEOUT_MS
    ) as consent_response:
        page.click(_SIGN_IN_BUTTON)
    if not consent_response.value.ok:
        raise SmokeAppError(
            f"The app answered {consent_response.value.status} to the sign-in page's error-reporting answer."
        )
    logger.info("Error reporting is declined on the sign-in page; Sign in is pressed")
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


def _require_signed_in_account_id(session: StudioAppSession, email: str) -> str:
    account_id = _signed_in_account_id(session, email)
    if account_id is None:
        raise SmokeAppError(f"The app does not list {email} as a signed-in account.")
    return account_id


def wait_for_signed_in_account(session: StudioAppSession, email: str, timeout_seconds: float) -> str:
    """Wait for the app to list ``email`` as a signed-in account; return its user id."""
    deadline = time.monotonic() + timeout_seconds
    account_id = _signed_in_account_id(session, email)
    while account_id is None:
        if time.monotonic() >= deadline:
            raise SmokeAppError(f"The app does not list {email} as a signed-in account after {timeout_seconds}s.")
        threading.Event().wait(timeout=_POLL_SECONDS)
        account_id = _signed_in_account_id(session, email)
    logger.info("The app lists {} as a signed-in account", email)
    return account_id


def wait_for_first_workspace_creation_page(session: StudioAppSession) -> str:
    """Wait for the sign-in page to hand over to the creation page of the account's first workspace; return
    that page's heading.

    The sign-in page submits the create by itself for an account the connector says has no workspace. It
    sends an account the connector says has some to its home page, and stays up with the reason when the
    create is refused; either fails here, saying which.
    """
    page = session.page
    outcomes = (_CREATING_HEADING, _CREATE_REFUSED, _HOME_PAGE)
    try:
        page.wait_for_selector(", ".join(outcomes), state="visible", timeout=_FIRST_CREATE_SUBMIT_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise SmokeAppError(
            f"The app neither started the account's first workspace nor said why within "
            f"{_FIRST_CREATE_SUBMIT_TIMEOUT_MS / 1000:.0f}s of the sign-in; its sign-in page stood at stage "
            f"{page.evaluate(_SIGN_IN_STAGE_JS)!r} and the page at {page.url}"
        ) from exc
    if page.locator(_CREATING_HEADING).count() > 0:
        heading = page.locator(_CREATING_HEADING).inner_text().strip()
        logger.info("The app started the account's first workspace; its creation page reads {!r}", heading)
        return heading
    if page.locator(_CREATE_REFUSED).count() > 0:
        raise SmokeAppError(
            f"The app could not start the account's first workspace: {page.locator(_CREATE_REFUSED).inner_text()}"
        )
    raise SmokeAppError(
        "The app took the account to its home page instead of creating its first workspace, though the check "
        "made before the launch found the account with no pool lease and no active workspace record: either a "
        "record became active between that check and the sign-in, or the app went to its home page for an "
        "account the connector counts no workspace for."
    )


def wait_for_first_workspace(session: StudioAppSession) -> Frame:
    """Wait for the workspace the creation page is building, or for its failure; return the workspace's frame.

    The workspace loads under the app's cover, so its frame is there before anything of it can be seen.
    """
    workspace_frame = wait_for_workspace_ready_or_failure(session.browser, session.page, _CREATE_TIMEOUT_SECONDS)
    logger.info("The workspace is created and loads at {}", workspace_frame.url)
    return workspace_frame


def reach_provider_page(session: StudioAppSession, workspace_frame: Frame) -> Frame:
    """Wait for the cover over the new workspace to lower itself; return the frame of the provider page it reveals."""
    wait_out_first_run_cover(session.page)
    return first_run_providers_frame(workspace_frame, FIRST_RUN_PROVIDERS_TIMEOUT_SECONDS)


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
    """The chat a workspace's desktop shows once its first run is behind it: the conversation the creation
    page's seed opened. A page of the chat app's own, which is the journey's way onto that app's origin."""
    chat = chat_frame(workspace_frame, _CHAT_FRAME_TIMEOUT_SECONDS)
    logger.info("The workspace's welcome chat is framed at {}", chat.url)
    return chat


def mint_workspace_llm_credentials(session: StudioAppSession, agent_id: str) -> str:
    """Mint the account's LiteLLM key for the workspace through the desktop, as the AI-keys modal does."""
    answer = fetch_from_app(session, HTTPMethod.POST, _MINT_AI_KEY_PATH, {"workspace": agent_id})
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

    ``chat`` is any page of the chat app's, the provider page among them.
    ``/api/claude-auth/submit-credentials`` is the chat app's endpoint for
    exactly this blob -- the cross-repo contract the desktop's AI-keys page
    mints for -- and it answers with the auth mode it understood, so a key
    that was read as a plain Anthropic key (routing off the Imbue proxy
    rather than through it) fails here rather than at the first reply. The
    chooser's own lane, whose paste textarea takes this blob, is not driven
    yet; that is a follow-up recorded in the spec.
    """
    answer = _fetch_in_chat(chat, HTTPMethod.POST, _SUBMIT_CREDENTIALS_PATH, {"credentials": credential_blob})
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


def wait_for_account_on_provider_page(providers: Frame, account_id: str) -> None:
    """Wait for the provider page to list the account under its "Connected" heading."""
    row = FIRST_RUN_ACCOUNT_SELECTOR_TEMPLATE.format(account_id=account_id)
    try:
        providers.wait_for_selector(row, state="visible", timeout=_ACCOUNT_LISTED_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise SmokeAppError(
            f"The provider page did not list account {account_id} as connected within "
            f"{_ACCOUNT_LISTED_TIMEOUT_MS / 1000:.0f}s of the workspace adopting it."
        ) from exc
    logger.info("The provider page lists account {} as connected", account_id)


def send_prompt_to_main_chat(workspace_frame: Frame, welcome_chat: Frame, prompt: str, marker: str) -> str:
    """Send ``prompt`` with the desktop launcher's Enter, which sends a text to the workspace's main chat; return that
    chat's id once its page shows the prompt (``marker`` is a string only the prompt carries).

    A fresh workspace's main chat is its welcome chat, open on the first conversation's ways to start: the prompt is
    none of them, so its agent takes it as an ordinary request, on the account connected on the provider page.
    """
    main_chat_id = _main_chat_id(welcome_chat, _MAIN_CHAT_TIMEOUT_SECONDS)
    send_prompt_to_known_main_chat(workspace_frame, main_chat_id, prompt, marker)
    return main_chat_id


def send_prompt_to_known_main_chat(workspace_frame: Frame, main_chat_id: str, prompt: str, marker: str) -> None:
    """Send ``prompt`` with the desktop launcher's Enter to the workspace's main chat, whose id the caller already has;
    return once that chat's page shows the prompt (``marker`` is a string only this prompt carries)."""
    send_to_main_chat_from_launcher(workspace_frame, prompt)
    _wait_until_chat_shows(
        workspace_frame, main_chat_id, _PROMPT_SHOWN_JS, marker, _PROMPT_SHOWN_TIMEOUT_SECONDS, "the prompt"
    )
    logger.info("Confirmed that the main chat {} shows the prompt", main_chat_id)


def _main_chat_id(chat: Frame, timeout_seconds: float) -> str:
    """The workspace's main chat, as the chat app's listing names it."""
    deadline = time.monotonic() + timeout_seconds
    last_outcome = "not asked"
    while time.monotonic() < deadline:
        answer = _fetch_in_chat(chat, HTTPMethod.GET, _CHATS_PATH, None)
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


def stop_workspace_from_account_home(
    session: StudioAppSession, account_email: str, agent_id: str, timeout_seconds: float
) -> None:
    """Open the account's home page and stop the workspace from its row there: press Stop and agree to its question.

    Returns once the row shows the Stopped badge. An Imbue Cloud machine can
    show it while its disks are still uploading, which the connector's own
    status tells apart.
    """
    account_id = _require_signed_in_account_id(session, account_email)
    open_account_home_page(session.page, str(session.backend_origin), account_id)
    stop_workspace_from_home_page(session.page, agent_id, timeout_seconds)


def _shown_text(locator: Locator) -> str:
    """What the elements of ``locator`` read, on one line; empty when the page shows none of them."""
    return " ".join(" ".join(locator.all_inner_texts()).split())


def _share_notices_clause(page: Page) -> str:
    """What the Sharing page says stands in the way, as the closing clause of a failure's message."""
    notices = _shown_text(page.locator(", ".join(_SHARE_NOTICES)))
    return f"the page says {notices!r}" if notices else "the page shows no notice of why"


def grant_share_from_panel(session: StudioAppSession, agent_id: str, grantee_email: str) -> str:
    """Grant the whole workspace to ``grantee_email`` from Settings' Sharing page; return what the grant's
    card says became of the invitation the grant sends.

    An invitation the tier refused or could not send is an answer like any other: the grant is saved by
    then. An entry the page will not take, or a grant it cannot save, raises with the page's own sentence.
    """
    page = session.page
    page.goto(f"{session.backend_origin}{_SHARE_TAB_PATH.format(agent_id=agent_id)}", wait_until="domcontentloaded")

    # The add row is live from the page's first draw, while the read that settles the page is still out, and
    # a grant made then is saved alongside that read.
    try:
        page.wait_for_selector(_GRANT_ADD_BUTTON_ONCE_SETTLED, state="visible", timeout=_SHARE_PAGE_SETTLE_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise SmokeAppError(
            f"The Sharing page at {page.url} did not settle on a shared workspace that takes grants within "
            f"{_SHARE_PAGE_SETTLE_TIMEOUT_MS / 1000:.0f}s; {_share_notices_clause(page)}"
        ) from exc

    # The page answers Add at once: the address joins the list as a card, or the row says why it cannot.
    card = page.locator(_GRANT_CARD, has_text=grantee_email)
    refusal = page.locator(_GRANT_ADD_REFUSAL)
    try:
        page.fill(_GRANT_ADD_VALUE, grantee_email)
        page.click(_GRANT_ADD_BUTTON)
        card.or_(refusal).first.wait_for(state="visible", timeout=_PAGE_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise SmokeAppError(
            f"Adding {grantee_email} on the Sharing page got neither a card nor a refusal; "
            f"{_share_notices_clause(page)}"
        ) from exc
    if refusal.count() > 0:
        raise SmokeAppError(f"The Sharing page would not grant {agent_id} to {grantee_email}: {_shown_text(refusal)}")

    # The card saves the grant, and a saved grant asks for its invitation.
    save_failure = card.locator(_GRANT_SAVE_FAILURE)
    invitation_answer = card.locator(_GRANT_INVITATION_ANSWER)
    try:
        save_failure.or_(invitation_answer).first.wait_for(state="visible", timeout=_GRANT_TIMEOUT_MS)
    except PlaywrightTimeoutError as exc:
        raise SmokeAppError(
            f"The grant of {agent_id} to {grantee_email} did not say what became of its invitation within "
            f"{_GRANT_TIMEOUT_MS / 1000:.0f}s; its card reads {_shown_text(card)!r}, and {_share_notices_clause(page)}"
        ) from exc
    if save_failure.count() > 0:
        raise SmokeAppError(
            f"The Sharing page could not save the grant of {agent_id} to {grantee_email}: "
            f"{save_failure.first.get_attribute(_TOOLTIP_ATTRIBUTE) or _shown_text(card)}"
        )
    invitation = _shown_text(invitation_answer)
    logger.info("Workspace {} is granted to {}, whose card reads {!r}", agent_id, grantee_email, _shown_text(card))
    return invitation


def unpublish_share_from_panel(session: StudioAppSession, agent_id: str) -> None:
    """Turn sharing off from Settings' Sharing page and wait for the page to show it off.

    The switch pointing off, with the link's row gone from the section that
    holds it, is what the page shows every workspace whose sharing was turned
    off.
    """
    page = session.page
    page.goto(f"{session.backend_origin}{_SHARE_TAB_PATH.format(agent_id=agent_id)}", wait_until="domcontentloaded")
    page.wait_for_selector(_PUBLISH_SWITCH_ON, state="visible", timeout=_PAGE_TIMEOUT_MS)
    page.click(_PUBLISH_SWITCH)
    page.wait_for_selector(_PUBLISH_SWITCH_SETTLED_OFF, state="visible", timeout=_UNPUBLISH_TIMEOUT_MS)
    page.wait_for_selector(_SHARE_LINK, state="detached", timeout=_PAGE_TIMEOUT_MS)
    logger.info("Workspace {} is no longer published", agent_id)


def read_sharing_status(session: StudioAppSession, agent_id: str) -> dict[str, Any]:
    status = fetch_json_from_app(session, _SHARING_PATH.format(agent_id=agent_id))
    if not isinstance(status, dict):
        raise SmokeAppError(f"The sharing status of {agent_id} is not an object: {str(status)[:200]}")
    return status


@pure
def is_sharing_enabled(status: Mapping[str, Any]) -> bool:
    """Whether the status says the workspace is shared, and names the host the share is on."""
    return status.get("enabled") is True and bool(status.get("host_id"))


def wait_for_sharing_enabled(session: StudioAppSession, agent_id: str, timeout_seconds: float) -> dict[str, Any]:
    """Wait for the workspace's share to report enabled; return that status.

    The first run's create shares the workspace it makes, in a step of its own after the workspace is up.
    """
    deadline = time.monotonic() + timeout_seconds
    status = read_sharing_status(session, agent_id)
    while not is_sharing_enabled(status):
        if time.monotonic() >= deadline:
            raise SmokeAppError(
                f"The create did not turn sharing on for {agent_id} within {timeout_seconds:.0f}s; "
                f"last status: enabled={status.get('enabled')}, host_id={status.get('host_id')}, "
                f"url={status.get('url')}"
            )
        threading.Event().wait(timeout=_POLL_SECONDS)
        status = read_sharing_status(session, agent_id)
    logger.info("Workspace {} is shared from its create, on host {}", agent_id, status.get("host_id"))
    return status


@pure
def share_url_for_service(status: Mapping[str, Any], service: str) -> str | None:
    """The link to one share target, or None until the share has a URL and the target a label.

    The status carries the share's base URL (whose bare domain does not route)
    and a label per registered service; the link that routes is
    ``https://<label>.<domain>/``, the way the Sharing page builds it.
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
    """Press Destroy, then confirm, on the workspace's General page in Settings."""
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
    last_answer: PageFetchAnswer | None = None
    while time.monotonic() < deadline:
        # The chrome page routes away from Settings once the destroy is
        # confirmed, which can tear down the document a fetch was evaluated in.
        try:
            if is_workspace_gone(list_workspaces(session), agent_id):
                logger.info("The destroy of workspace {} is done", agent_id)
                return
            last_answer = fetch_from_app(
                session, HTTPMethod.GET, _DESTROY_OPERATION_PATH.format(agent_id=agent_id), None
            )
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
