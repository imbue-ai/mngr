"""Create a Telegram bot via BotFather using credentials from latchkey.

Usage:
    python scripts/create_telegram_bot.py <bot_display_name> <bot_username>

    bot_display_name: The human-readable name for the bot (e.g. "My Cool Bot")
    bot_username: The username for the bot, must end in 'bot' (e.g. "my_cool_bot")

Credential sources (checked in order):
    1. TELEGRAM_STRING_SESSION env var: a pre-built Telethon StringSession
    2. TELEGRAM_DC_ID + TELEGRAM_AUTH_KEY_HEX env vars: raw MTProto auth data
    3. latchkey auth get telegram: reads from latchkey's encrypted store
    4. /tmp/latchkey-telegram-dump.json: fallback dump file

To set up credentials, run: latchkey auth browser telegram

The script connects to Telegram as the user, sends commands to @BotFather
to create a new bot, and prints the resulting bot token and username to stdout.
All status/error messages go to stderr.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import click
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

DC_IPS = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}

TELEGRAM_WEB_URL = "https://web.telegram.org/a/"

LATCHKEY_DUMP_PATH = Path("/tmp/latchkey-telegram-dump.json")


class TelegramCredentialError(Exception):
    """Raised when Telegram credentials cannot be resolved."""

    ...


class BotCreationError(Exception):
    """Raised when BotFather rejects or returns an unexpected response."""

    ...


def _fetch_telegram_web_api_credentials() -> tuple[int, str]:
    """Extract api_id and api_hash from the live Telegram Web A JS bundles.

    These are public application identifiers embedded as literals in the
    minified JavaScript. We search for the pattern
    Number("DIGITS"),"32-hex-char-hash" from the TelegramClient constructor.

    Searches the main bundle first, then iterates webpack chunks until found.
    Falls back to known defaults if extraction fails for any reason.
    """
    try:
        html = urllib.request.urlopen(TELEGRAM_WEB_URL).read().decode()

        main_match = re.search(r"(main\.[a-f0-9]+\.js)", html)
        if not main_match:
            raise ValueError("Could not find main bundle in Telegram Web HTML")

        main_js_url = TELEGRAM_WEB_URL + main_match.group(1)
        main_js = urllib.request.urlopen(main_js_url).read().decode()

        cred_match = re.search(r'Number\("(\d+)"\),"([a-f0-9]{32})"', main_js)
        if cred_match:
            return int(cred_match.group(1)), cred_match.group(2)

        chunk_entries = re.findall(r'(\d+):"([a-f0-9]{16,})"', main_js)
        for chunk_id, chunk_hash in chunk_entries:
            chunk_url = f"{TELEGRAM_WEB_URL}{chunk_id}.{chunk_hash}.js"
            try:
                chunk_js = urllib.request.urlopen(chunk_url).read().decode()
            except (urllib.error.URLError, urllib.error.HTTPError):
                continue
            cred_match = re.search(r'Number\("(\d+)"\),"([a-f0-9]{32})"', chunk_js)
            if cred_match:
                return int(cred_match.group(1)), cred_match.group(2)

        raise ValueError("Credential pattern not found in any bundle chunk")

    except (urllib.error.URLError, OSError, ValueError) as exc:
        sys.stderr.write(
            f"Warning: could not extract api credentials from Telegram Web bundle ({exc}), using known defaults\n"
        )
        return 2496, "8da85b0d5bfe62527e5b244c209159c3"


def _auth_key_to_string_session(dc_id: int, auth_key_hex: str) -> str:
    """Convert a dc_id + auth_key_hex to a Telethon StringSession string.

    Raises TelegramCredentialError if the inputs are invalid.
    """
    ip_str = DC_IPS.get(dc_id)
    if ip_str is None:
        raise TelegramCredentialError(f"Unknown data center ID {dc_id}")
    ip_packed = ipaddress.ip_address(ip_str).packed
    try:
        auth_key_bytes = bytes.fromhex(auth_key_hex)
    except ValueError as exc:
        raise TelegramCredentialError(f"auth_key is not valid hex: {exc}") from exc
    if len(auth_key_bytes) != 256:
        raise TelegramCredentialError(f"auth_key must be 256 bytes, got {len(auth_key_bytes)}")
    fmt = f">B{len(ip_packed)}sH256s"
    packed = struct.pack(fmt, dc_id, ip_packed, 443, auth_key_bytes)
    return "1" + base64.urlsafe_b64encode(packed).decode("ascii")


def _try_latchkey_auth_get() -> str | None:
    """Try to read Telegram user credentials from latchkey's credential store.

    Returns a StringSession string if successful, None if latchkey is not
    available or has no Telegram credentials.
    """
    latchkey_path = shutil.which("latchkey")
    if latchkey_path is None:
        return None

    try:
        result = subprocess.run(
            [latchkey_path, "auth", "get", "telegram"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None

        creds = json.loads(result.stdout)
        if creds.get("objectType") != "telegramUser":
            return None

        dc_id = creds["dcId"]
        auth_key_hex = creds["authKeyHex"]
        sys.stderr.write(f"Read credentials from latchkey (user={creds.get('firstName', '?')}, DC={dc_id})\n")
        return _auth_key_to_string_session(dc_id, auth_key_hex)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
        return None


def _get_string_session() -> str:
    """Resolve a Telethon StringSession from available credential sources.

    Checks (in order): TELEGRAM_STRING_SESSION env var, TELEGRAM_DC_ID +
    TELEGRAM_AUTH_KEY_HEX env vars, latchkey auth get, latchkey dump file.

    Raises TelegramCredentialError if no credentials are found.
    """
    # Option 1: direct StringSession
    session = os.environ.get("TELEGRAM_STRING_SESSION")
    if session:
        return session

    # Option 2: dc_id + auth_key_hex
    dc_id_str = os.environ.get("TELEGRAM_DC_ID")
    auth_key_hex = os.environ.get("TELEGRAM_AUTH_KEY_HEX")
    if dc_id_str and auth_key_hex:
        try:
            dc_id = int(dc_id_str)
        except ValueError as exc:
            raise TelegramCredentialError(f"TELEGRAM_DC_ID must be an integer, got {dc_id_str!r}") from exc
        return _auth_key_to_string_session(dc_id, auth_key_hex)

    # Option 3: latchkey auth get
    session = _try_latchkey_auth_get()
    if session:
        return session

    # Option 4: latchkey dump file (fallback for older latchkey versions)
    if LATCHKEY_DUMP_PATH.exists():
        sys.stderr.write(f"Reading credentials from {LATCHKEY_DUMP_PATH}\n")
        dump = json.loads(LATCHKEY_DUMP_PATH.read_text())
        ls = dump.get("localStorage", {})
        dc_str = ls.get("dc")
        user_auth_str = ls.get("user_auth")
        if not dc_str or not user_auth_str:
            raise TelegramCredentialError(f"{LATCHKEY_DUMP_PATH} does not contain valid auth data")
        try:
            dc_id = int(dc_str)
        except ValueError as exc:
            raise TelegramCredentialError(
                f"'dc' value in {LATCHKEY_DUMP_PATH} must be an integer, got {dc_str!r}"
            ) from exc
        auth_key_raw = ls.get(f"dc{dc_id}_auth_key", "")
        # The value may be JSON-encoded (wrapped in extra quotes)
        if auth_key_raw.startswith('"'):
            try:
                auth_key_hex = json.loads(auth_key_raw)
            except json.JSONDecodeError as exc:
                raise TelegramCredentialError(
                    f"Could not parse auth_key for DC {dc_id} in {LATCHKEY_DUMP_PATH}: {exc}"
                ) from exc
        else:
            auth_key_hex = auth_key_raw
        if not auth_key_hex:
            raise TelegramCredentialError(f"No auth_key for DC {dc_id} in dump")
        return _auth_key_to_string_session(dc_id, auth_key_hex)

    raise TelegramCredentialError(
        "No Telegram credentials found.\n"
        "Provide credentials via one of:\n"
        "  - TELEGRAM_STRING_SESSION environment variable\n"
        "  - TELEGRAM_DC_ID + TELEGRAM_AUTH_KEY_HEX environment variables\n"
        "  - Run 'latchkey auth browser telegram' to store credentials"
    )


def create_bot(bot_display_name: str, bot_username: str) -> tuple[str, str]:
    """Create a Telegram bot via BotFather.

    Uses telethon.sync so all Telegram client methods run synchronously.

    Returns (bot_token, bot_username) on success.
    Raises BotCreationError on failure.
    """
    session_str = _get_string_session()
    api_id, api_hash = _fetch_telegram_web_api_credentials()
    sys.stderr.write(f"Using api_id={api_id}\n")

    client = TelegramClient(StringSession(session_str), api_id, api_hash)
    client.connect()

    try:
        if not client.is_user_authorized():
            raise TelegramCredentialError(
                "Telegram session is not authorized. The auth key may have been "
                "revoked.\nRun 'latchkey auth browser telegram' to log in again."
            )

        me = client.get_me()
        sys.stderr.write(f"Connected as: {me.first_name} (id={me.id})\n")

        botfather = client.get_entity("@BotFather")

        with client.conversation(botfather) as conv:
            # Step 1: Send /newbot and wait for name prompt
            conv.send_message("/newbot")
            resp = conv.get_response()
            if "choose a name" not in resp.text.lower():
                raise BotCreationError(f"Unexpected BotFather response to /newbot:\n{resp.text}")

            # Step 2: Send the display name and wait for username prompt
            conv.send_message(bot_display_name)
            resp = conv.get_response()
            if "username" not in resp.text.lower():
                raise BotCreationError(f"Unexpected BotFather response to bot name:\n{resp.text}")

            # Step 3: Send the username and wait for confirmation
            conv.send_message(bot_username)
            resp = conv.get_response()
            response_text = resp.text

            if "sorry" in response_text.lower() or "error" in response_text.lower():
                raise BotCreationError(f"BotFather rejected the username:\n{response_text}")

            # Parse the bot token from the response
            # BotFather sends: "Use this token to access the HTTP API:\n<id>:<hash>"
            token_match = re.search(r"(\d+:[A-Za-z0-9_-]+)", response_text)
            if not token_match:
                raise BotCreationError(f"Could not extract bot token from BotFather response:\n{response_text}")

            bot_token = token_match.group(1)

            # Extract the actual username from the response
            username_match = re.search(r"t\.me/(\w+)", response_text)
            actual_username = username_match.group(1) if username_match else bot_username

    finally:
        client.disconnect()

    return bot_token, actual_username


@click.command()
@click.argument("bot_display_name")
@click.argument("bot_username")
def main(bot_display_name: str, bot_username: str) -> None:
    """Create a Telegram bot via BotFather.

    BOT_DISPLAY_NAME is the human-readable name (e.g. 'My Cool Bot').

    BOT_USERNAME must end in 'bot' (e.g. 'my_cool_bot').
    """
    if not bot_username.lower().endswith("bot"):
        raise click.BadParameter(
            f"bot username must end in 'bot' (got '{bot_username}')",
            param_hint="'bot_username'",
        )

    try:
        bot_token, actual_username = create_bot(bot_display_name, bot_username)
    except (TelegramCredentialError, BotCreationError) as exc:
        raise click.ClickException(str(exc)) from exc

    # Print both values to stdout (all other output goes to stderr)
    sys.stdout.write(f"bot_token={bot_token}\n")
    sys.stdout.write(f"bot_username={actual_username}\n")


if __name__ == "__main__":
    main()
