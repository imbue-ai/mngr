"""Create Telegram bots via BotFather using extracted user credentials.

Connects to Telegram as the authenticated user via Telethon and
sends commands to @BotFather to create a new bot. Also provides
helpers for converting raw MTProto credentials into Telethon sessions
and fetching the public Telegram Web API credentials.
"""

import base64
import ipaddress
import re
import struct
import urllib.error
import urllib.request
from typing import Final

from loguru import logger
from pydantic import SecretStr
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

from imbue.imbue_common.logging import log_span
from imbue.minds.errors import TelegramBotCreationError
from imbue.minds.errors import TelegramCredentialError
from imbue.minds.telegram.data_types import TELEGRAM_WEB_URL
from imbue.minds.telegram.data_types import TelegramBotCredentials
from imbue.minds.telegram.data_types import TelegramUserCredentials

DC_IPS: Final[dict[int, str]] = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}

_FALLBACK_API_ID: Final[int] = 2496

_FALLBACK_API_HASH: Final[str] = "8da85b0d5bfe62527e5b244c209159c3"

_AUTH_KEY_BYTE_LENGTH: Final[int] = 256

_DEFAULT_PORT: Final[int] = 443

_HTTP_TIMEOUT_SECONDS: Final[int] = 15

_BOT_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\d+:[A-Za-z0-9_-]+)")

_BOT_USERNAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"t\.me/(\w+)")


def auth_key_to_string_session(dc_id: int, auth_key_hex: str) -> str:
    """Convert a dc_id + auth_key_hex to a Telethon StringSession string.

    Raises TelegramCredentialError if the inputs are invalid.
    """
    ip_str = DC_IPS.get(dc_id)
    if ip_str is None:
        raise TelegramCredentialError(f"Unknown Telegram data center ID: {dc_id}")

    ip_packed = ipaddress.ip_address(ip_str).packed

    try:
        auth_key_bytes = bytes.fromhex(auth_key_hex)
    except ValueError as exc:
        raise TelegramCredentialError(f"auth_key is not valid hex: {exc}") from exc

    if len(auth_key_bytes) != _AUTH_KEY_BYTE_LENGTH:
        raise TelegramCredentialError(f"auth_key must be {_AUTH_KEY_BYTE_LENGTH} bytes, got {len(auth_key_bytes)}")

    fmt = f">B{len(ip_packed)}sH{_AUTH_KEY_BYTE_LENGTH}s"
    packed = struct.pack(fmt, dc_id, ip_packed, _DEFAULT_PORT, auth_key_bytes)
    return "1" + base64.urlsafe_b64encode(packed).decode("ascii")


def fetch_telegram_web_api_credentials() -> tuple[int, str]:
    """Extract api_id and api_hash from the live Telegram Web A JS bundles.

    These are public application identifiers embedded in the minified
    JavaScript. Falls back to known defaults if extraction fails.
    """
    try:
        with urllib.request.urlopen(TELEGRAM_WEB_URL, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            html = resp.read().decode()

        main_match = re.search(r"(main\.[a-f0-9]+\.js)", html)
        if not main_match:
            raise ValueError("Could not find main bundle in Telegram Web HTML")

        main_js_url = TELEGRAM_WEB_URL + main_match.group(1)
        with urllib.request.urlopen(main_js_url, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
            main_js = resp.read().decode()

        cred_match = re.search(r'Number\("(\d+)"\),"([a-f0-9]{32})"', main_js)
        if cred_match:
            return int(cred_match.group(1)), cred_match.group(2)

        # Search webpack chunks if not found in main bundle
        chunk_entries = re.findall(r'(\d+):"([a-f0-9]{16,})"', main_js)
        for chunk_id, chunk_hash in chunk_entries:
            chunk_url = f"{TELEGRAM_WEB_URL}{chunk_id}.{chunk_hash}.js"
            try:
                with urllib.request.urlopen(chunk_url, timeout=_HTTP_TIMEOUT_SECONDS) as resp:
                    chunk_js = resp.read().decode()
            except (urllib.error.URLError, urllib.error.HTTPError):
                continue
            cred_match = re.search(r'Number\("(\d+)"\),"([a-f0-9]{32})"', chunk_js)
            if cred_match:
                return int(cred_match.group(1)), cred_match.group(2)

        raise ValueError("Credential pattern not found in any bundle chunk")

    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning(
            "Could not extract API credentials from Telegram Web bundle ({}), using known defaults",
            exc,
        )
        return _FALLBACK_API_ID, _FALLBACK_API_HASH


def create_telegram_bot(
    user_credentials: TelegramUserCredentials,
    bot_display_name: str,
    bot_username: str,
) -> TelegramBotCredentials:
    """Create a Telegram bot via BotFather using the given user credentials.

    Connects to Telegram as the user, sends commands to @BotFather to
    create a new bot with the given name and username.

    Raises TelegramCredentialError if the session is not authorized.
    Raises TelegramBotCreationError if BotFather rejects the request.
    """
    session_str = auth_key_to_string_session(
        dc_id=user_credentials.dc_id,
        auth_key_hex=user_credentials.auth_key_hex,
    )
    api_id, api_hash = fetch_telegram_web_api_credentials()

    with log_span("Creating Telegram bot '{}' via BotFather", bot_username):
        logger.debug("Using api_id={}", api_id)

        client = TelegramClient(StringSession(session_str), api_id, api_hash)
        client.connect()  # ty: ignore[unused-awaitable]

        try:
            if not client.is_user_authorized():  # ty: ignore[unused-awaitable]
                raise TelegramCredentialError(
                    "Telegram session is not authorized. The auth key may have been "
                    "revoked. Please log in again via the Setup Telegram button."
                )

            # telethon.sync patches async methods to return synchronous values at runtime,
            # but the type stubs still show coroutine return types
            me = client.get_me()  # ty: ignore[unused-awaitable]
            logger.debug("Connected as: {} (id={})", me.first_name, me.id)  # ty: ignore[unresolved-attribute]

            bot_token, actual_username = _converse_with_botfather(
                client=client,
                bot_display_name=bot_display_name,
                bot_username=bot_username,
            )
        finally:
            client.disconnect()  # ty: ignore[unused-awaitable]

    return TelegramBotCredentials(
        bot_token=SecretStr(bot_token),
        bot_username=actual_username,
    )


def _converse_with_botfather(
    client: TelegramClient,
    bot_display_name: str,
    bot_username: str,
) -> tuple[str, str]:
    """Send commands to @BotFather to create a new bot.

    Returns (bot_token, actual_username).
    """
    botfather = client.get_entity("@BotFather")  # ty: ignore[unused-awaitable]

    with client.conversation(botfather) as conv:  # ty: ignore[invalid-argument-type]
        # Step 1: initiate bot creation
        conv.send_message("/newbot")
        resp = conv.get_response()
        if "choose a name" not in resp.text.lower():
            raise TelegramBotCreationError(f"Unexpected BotFather response to /newbot:\n{resp.text}")

        # Step 2: send the display name
        conv.send_message(bot_display_name)
        resp = conv.get_response()
        if "username" not in resp.text.lower():
            raise TelegramBotCreationError(f"Unexpected BotFather response to bot name:\n{resp.text}")

        # Step 3: send the username
        conv.send_message(bot_username)
        resp = conv.get_response()
        response_text = resp.text

        if "sorry" in response_text.lower() or "error" in response_text.lower():
            raise TelegramBotCreationError(f"BotFather rejected the username:\n{response_text}")

        # Extract the bot token from the response
        token_match = _BOT_TOKEN_PATTERN.search(response_text)
        if not token_match:
            raise TelegramBotCreationError(f"Could not extract bot token from BotFather response:\n{response_text}")

        bot_token = token_match.group(1)

        # Extract the actual username from the response
        username_match = _BOT_USERNAME_PATTERN.search(response_text)
        actual_username = username_match.group(1) if username_match else bot_username

    return bot_token, actual_username
