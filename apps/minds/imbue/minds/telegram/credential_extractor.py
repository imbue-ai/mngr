"""Extract Telegram MTProto credentials from web.telegram.org via Playwright.

Opens a visible browser window to web.telegram.org/a/ and waits for the user
to log in. Once the Telegram Web A client has stored auth credentials in
localStorage, extracts them and returns a TelegramUserCredentials object.
"""

import json
from typing import Final

from loguru import logger
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from imbue.imbue_common.logging import log_span
from imbue.minds.errors import TelegramCredentialExtractionError
from imbue.minds.telegram.data_types import TELEGRAM_WEB_URL
from imbue.minds.telegram.data_types import TelegramUserCredentials

_AUTH_KEY_HEX_LENGTH: Final[int] = 512

_DEFAULT_LOGIN_TIMEOUT_SECONDS: Final[int] = 300


def extract_telegram_credentials_from_browser(
    login_timeout_seconds: int = _DEFAULT_LOGIN_TIMEOUT_SECONDS,
) -> TelegramUserCredentials:
    """Open a browser to web.telegram.org and extract MTProto user credentials.

    Launches a visible Chromium browser so the user can log in manually.
    Automatically detects when login is complete by polling localStorage
    for the appearance of auth data. Extracts the dc_id, auth_key, user_id,
    and first_name from the browser's localStorage.

    Raises TelegramCredentialExtractionError if login times out, credentials
    are missing, or the auth key has an unexpected format.
    """
    with log_span("Extracting Telegram credentials from browser"):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            try:
                page = browser.new_page()
                page.goto(TELEGRAM_WEB_URL)

                logger.info("Waiting for user to log in to Telegram Web...")

                # Wait for auth data to appear in localStorage
                try:
                    page.wait_for_function(
                        """() => {
                            const dc = localStorage.getItem('dc');
                            const userAuth = localStorage.getItem('user_auth');
                            return dc !== null && userAuth !== null;
                        }""",
                        timeout=login_timeout_seconds * 1000,
                    )
                except PlaywrightTimeoutError as exc:
                    raise TelegramCredentialExtractionError(
                        f"Timed out waiting for Telegram login after {login_timeout_seconds} seconds. "
                        "Make sure you complete the login process in the browser window."
                    ) from exc

                credentials = _extract_credentials_from_page(page)
            finally:
                browser.close()

    logger.info(
        "Extracted Telegram credentials for {} (user_id={}, DC={})",
        credentials.first_name,
        credentials.user_id,
        credentials.dc_id,
    )
    return credentials


def _extract_credentials_from_page(page: Page) -> TelegramUserCredentials:
    """Extract credentials from a logged-in Telegram Web page's localStorage.

    This is a thin shell around the live Playwright ``Page``: it pulls the raw
    localStorage strings out of the browser and delegates all parsing/validation
    to the pure helpers below (which are unit-tested directly).
    """
    # Extract dc and user_auth
    auth_data = page.evaluate(
        """(() => {
            const dc = localStorage.getItem('dc');
            const userAuth = localStorage.getItem('user_auth');
            return { dc: dc, userAuth: userAuth };
        })()"""
    )

    dc_id, user_id = _parse_dc_id_and_user_id(auth_data.get("dc"), auth_data.get("userAuth"))

    # Extract the auth_key for the active DC (the localStorage key depends on dc_id)
    dc_key_name = f"dc{dc_id}_auth_key"
    auth_key_raw = page.evaluate(f"localStorage.getItem('{dc_key_name}')")
    auth_key_hex = _parse_auth_key_hex(auth_key_raw, dc_id)

    account_data = page.evaluate("localStorage.getItem('account1')")
    first_name = _parse_first_name(account_data)

    return TelegramUserCredentials(
        dc_id=dc_id,
        auth_key_hex=auth_key_hex,
        user_id=user_id,
        first_name=first_name,
    )


def _parse_dc_id_and_user_id(dc_str: str | None, user_auth_str: str | None) -> tuple[int, str]:
    """Parse the data-center ID and user ID out of the raw localStorage strings.

    ``dc_str`` is the ``dc`` entry and ``user_auth_str`` is the ``user_auth``
    JSON entry. Raises TelegramCredentialExtractionError if either is missing or
    malformed, or if the user ID is absent.
    """
    if not dc_str or not user_auth_str:
        raise TelegramCredentialExtractionError(
            "Could not find Telegram auth data in localStorage. "
            "Make sure you are fully logged in (you should see your chat list)."
        )

    try:
        dc_id = int(dc_str)
    except ValueError as exc:
        raise TelegramCredentialExtractionError(f"Invalid data center ID in localStorage: {dc_str!r}") from exc

    try:
        user_auth = json.loads(user_auth_str)
    except json.JSONDecodeError as exc:
        raise TelegramCredentialExtractionError(f"Could not parse user_auth from localStorage: {exc}") from exc

    user_id = str(user_auth.get("id", ""))
    if not user_id:
        raise TelegramCredentialExtractionError("user_auth in localStorage does not contain a user ID")

    return dc_id, user_id


def _parse_auth_key_hex(auth_key_raw: str | None, dc_id: int) -> str:
    """Validate and normalize the raw ``dc{dc_id}_auth_key`` localStorage value.

    The stored value may be a bare hex string or a JSON-encoded string (wrapped
    in extra quotes). Raises TelegramCredentialExtractionError if it is missing,
    unparseable, or not exactly ``_AUTH_KEY_HEX_LENGTH`` hex characters.
    """
    if not auth_key_raw:
        raise TelegramCredentialExtractionError(
            f"Could not find auth key for DC {dc_id} in localStorage (key: dc{dc_id}_auth_key)"
        )

    # The value may be JSON-encoded (wrapped in extra quotes)
    if auth_key_raw.startswith('"'):
        try:
            auth_key_hex = json.loads(auth_key_raw)
        except json.JSONDecodeError as exc:
            raise TelegramCredentialExtractionError(f"Could not parse auth_key for DC {dc_id}: {exc}") from exc
    else:
        auth_key_hex = auth_key_raw

    if len(auth_key_hex) != _AUTH_KEY_HEX_LENGTH:
        raise TelegramCredentialExtractionError(
            f"Auth key has unexpected length: {len(auth_key_hex)} hex chars (expected {_AUTH_KEY_HEX_LENGTH})"
        )

    return auth_key_hex


def _parse_first_name(account_data: str | None) -> str:
    """Extract the user's first name from the raw ``account1`` localStorage value.

    Returns an empty string (and logs a warning) if the value is absent or
    cannot be parsed -- the first name is best-effort and never fatal.
    """
    if not account_data:
        return ""
    try:
        parsed_account = json.loads(account_data)
        return parsed_account.get("firstName", "")
    except (json.JSONDecodeError, AttributeError) as exc:
        logger.warning("Could not parse account1 data for first name: {}", exc)
        return ""
