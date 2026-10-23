"""The suite's own browser, for what a user does outside the app.

The app's web-login flow hands the user to the tier's hosted accounts page in
the system browser, and a share link is opened by whoever received it. Both
are driven here in a headless Chromium the suite launches itself, with the
ids the hosted sign-in page carries (``AuthPage.ts`` in the connector's
frontend). Playwright finds that Chromium under the home directory it was
installed to, which is why the browsers directory is resolved explicitly
rather than left to the (test-redirected) ``HOME``.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from loguru import logger
from playwright.sync_api import BrowserContext
from playwright.sync_api import BrowserType
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from imbue.imbue_common.pure import pure
from imbue.minds.smoke_tests.data_types import SmokeAccountCredentials
from imbue.minds.smoke_tests.primitives import SmokeConfigError

# How Playwright is told where its browsers live; unset, it looks under HOME.
PLAYWRIGHT_BROWSERS_PATH_ENV_VAR: Final[str] = "PLAYWRIGHT_BROWSERS_PATH"
_DARWIN_PLATFORM_NAME: Final[str] = "darwin"

# The hosted sign-in page (``AuthPage.ts``): the credentials form, the
# "Continue as <email>" interstitial an existing session is offered, and the
# reveal link a Google-enabled tier hides the form behind.
_EMAIL_FIELD: Final[str] = "#email"
_PASSWORD_FIELD: Final[str] = "#password"
_SUBMIT_BUTTON: Final[str] = "#auth-submit-btn"
_CONTINUE_AS_BUTTON: Final[str] = "#continue-as-btn"
_REVEAL_FORM_BUTTON: Final[str] = "#reveal-email-form-btn"
_ANY_LOGIN_CONTROL: Final[str] = f"{_SUBMIT_BUTTON}, {_CONTINUE_AS_BUTTON}, {_REVEAL_FORM_BUTTON}"
_LOGIN_PAGE_TIMEOUT_MS: Final[float] = 60_000.0
_INSTALL_HINT: Final[str] = "run `uv run playwright install chromium`"


@pure
def default_playwright_browsers_dir(home: Path, platform_name: str) -> Path:
    """Where ``playwright install`` puts browsers when ``PLAYWRIGHT_BROWSERS_PATH`` is unset."""
    if platform_name == _DARWIN_PLATFORM_NAME:
        return home / "Library" / "Caches" / "ms-playwright"
    return home / ".cache" / "ms-playwright"


@contextmanager
def visitor_browser_context(browser_type: BrowserType) -> Iterator[BrowserContext]:
    """A fresh headless Chromium context: the browser a user would sign in and open a share link with.

    Launched through the Playwright the app session already runs (its
    ``Browser.browser_type``): the sync API allows one driver per thread, so a
    second ``sync_playwright()`` beside the app's would refuse to start.
    """
    try:
        browser = browser_type.launch()
    except PlaywrightError as exc:
        raise SmokeConfigError(f"Playwright could not launch Chromium ({_INSTALL_HINT}): {exc}") from exc
    try:
        yield browser.new_context()
    finally:
        browser.close()


def is_hosted_login_page(page: Page) -> bool:
    """Whether ``page`` is showing the hosted sign-in rather than what it was asked for.

    What a share link answers a browser the relay does not recognize, which is
    why a visit asserts on this before it asserts on the page it wanted.
    """
    return page.locator(_ANY_LOGIN_CONTROL).count() > 0


def complete_hosted_login(page: Page, credentials: SmokeAccountCredentials) -> None:
    """Sign in on the hosted accounts page ``page`` is showing, whichever of its forms it offers.

    A browser already holding a session is offered "Continue as"; one without
    is shown the credentials form (behind a reveal link on a tier that leads
    with Google). Returns once the submit has been pressed; what the page
    navigates to next is the caller's to wait for.
    """
    page.wait_for_selector(_ANY_LOGIN_CONTROL, state="visible", timeout=_LOGIN_PAGE_TIMEOUT_MS)
    if page.locator(_CONTINUE_AS_BUTTON).count() > 0:
        logger.info("The hosted login page offers to continue as the signed-in account; accepting")
        page.click(_CONTINUE_AS_BUTTON)
        return
    if page.locator(_REVEAL_FORM_BUTTON).count() > 0 and page.locator(_SUBMIT_BUTTON).count() == 0:
        page.click(_REVEAL_FORM_BUTTON)
        page.wait_for_selector(_SUBMIT_BUTTON, state="visible", timeout=_LOGIN_PAGE_TIMEOUT_MS)
    logger.info("Signing in on the hosted login page as {}", credentials.email)
    page.fill(_EMAIL_FIELD, str(credentials.email))
    page.fill(_PASSWORD_FIELD, credentials.password.get_secret_value())
    page.click(_SUBMIT_BUTTON)
