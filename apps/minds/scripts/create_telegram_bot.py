"""Create a Telegram bot via BotFather using credentials from latchkey.

Usage:
    python scripts/create_telegram_bot.py <bot_display_name> <bot_username>

    bot_display_name: The human-readable name for the bot (e.g. "My Cool Bot")
    bot_username: The username for the bot, must end in 'bot' (e.g. "my_cool_bot")

Environment variables:
    TELEGRAM_DC_ID: The Telegram data center ID (e.g. "3")
    TELEGRAM_AUTH_KEY_HEX: The 512-char hex MTProto auth_key

    Alternatively:
    TELEGRAM_STRING_SESSION: A pre-built Telethon StringSession string

    If neither is set, falls back to reading /tmp/latchkey-telegram-dump.json
    (the file produced by `latchkey auth browser telegram`).

Prerequisites:
    - telethon must be installed: pip install telethon
    - Telegram user credentials, provided via one of the methods above

The script connects to Telegram as the user, sends commands to @BotFather
to create a new bot, and prints the resulting bot token and username to stdout.
All status/error messages go to stderr.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import re
import struct
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

DC_IPS = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}

TELEGRAM_WEB_URL = "https://web.telegram.org/a/"

LATCHKEY_DUMP_PATH = Path("/tmp/latchkey-telegram-dump.json")


def _fetch_telegram_web_api_credentials() -> tuple[int, str]:
    """Extract api_id and api_hash from the live Telegram Web A JS bundles.

    Telegram Web embeds these as literals in its minified JavaScript (they're
    public application identifiers, not secrets). The pattern in the minified
    code is Number("DIGITS"),"32-hex-char-hash" from the TelegramClient
    constructor call.

    We fetch the main HTML, extract all JS bundle URLs, then search each
    bundle for the credential pattern. This is chunk-number-agnostic -- the
    credentials could move to any bundle across releases.
    """
    import urllib.request

    try:
        html = urllib.request.urlopen(TELEGRAM_WEB_URL).read().decode()

        # Find the main bundle to get the webpack chunk map
        main_match = re.search(r"(main\.[a-f0-9]+\.js)", html)
        if not main_match:
            raise ValueError("Could not find main bundle in Telegram Web HTML")

        main_js_url = TELEGRAM_WEB_URL + main_match.group(1)
        main_js = urllib.request.urlopen(main_js_url).read().decode()

        # First check the main bundle itself
        cred_match = re.search(r'Number\("(\d+)"\),"([a-f0-9]{32})"', main_js)
        if cred_match:
            api_id = int(cred_match.group(1))
            api_hash = cred_match.group(2)
            print(f"Extracted api_id={api_id} from Telegram Web bundle", file=sys.stderr)
            return api_id, api_hash

        # Extract all chunk hashes from the webpack chunk map.
        # The map contains entries like  42:"a1b2c3d4e5f6..."
        chunk_entries = re.findall(r'(\d+):"([a-f0-9]{16,})"', main_js)

        for chunk_id, chunk_hash in chunk_entries:
            chunk_url = f"{TELEGRAM_WEB_URL}{chunk_id}.{chunk_hash}.js"
            try:
                chunk_js = urllib.request.urlopen(chunk_url).read().decode()
            except Exception:
                continue
            cred_match = re.search(r'Number\("(\d+)"\),"([a-f0-9]{32})"', chunk_js)
            if cred_match:
                api_id = int(cred_match.group(1))
                api_hash = cred_match.group(2)
                print(
                    f"Extracted api_id={api_id} from Telegram Web bundle",
                    file=sys.stderr,
                )
                return api_id, api_hash

        raise ValueError("Credential pattern not found in any bundle chunk")

    except Exception as exc:
        print(
            f"Warning: could not extract api credentials from Telegram Web "
            f"bundle ({exc}), using known defaults",
            file=sys.stderr,
        )
        return 2496, "8da85b0d5bfe62527e5b244c209159c3"


def _auth_key_to_string_session(dc_id: int, auth_key_hex: str) -> str:
    """Convert a dc_id + auth_key_hex to a Telethon StringSession string."""
    ip_str = DC_IPS.get(dc_id)
    if ip_str is None:
        print(f"Error: unknown data center ID {dc_id}", file=sys.stderr)
        sys.exit(1)
    ip_packed = ipaddress.ip_address(ip_str).packed
    auth_key_bytes = bytes.fromhex(auth_key_hex)
    if len(auth_key_bytes) != 256:
        print(
            f"Error: auth_key must be 256 bytes, got {len(auth_key_bytes)}",
            file=sys.stderr,
        )
        sys.exit(1)
    fmt = f">B{len(ip_packed)}sH256s"
    packed = struct.pack(fmt, dc_id, ip_packed, 443, auth_key_bytes)
    return "1" + base64.urlsafe_b64encode(packed).decode("ascii")


def _get_string_session() -> str:
    """Resolve a Telethon StringSession from available sources."""
    # Option 1: direct StringSession
    session = os.environ.get("TELEGRAM_STRING_SESSION")
    if session:
        return session

    # Option 2: dc_id + auth_key_hex
    dc_id_str = os.environ.get("TELEGRAM_DC_ID")
    auth_key_hex = os.environ.get("TELEGRAM_AUTH_KEY_HEX")
    if dc_id_str and auth_key_hex:
        return _auth_key_to_string_session(int(dc_id_str), auth_key_hex)

    # Option 3: latchkey dump file
    if LATCHKEY_DUMP_PATH.exists():
        print(f"Reading credentials from {LATCHKEY_DUMP_PATH}", file=sys.stderr)
        with open(LATCHKEY_DUMP_PATH) as f:
            dump = json.load(f)
        ls = dump.get("localStorage", {})
        dc_str = ls.get("dc")
        user_auth_str = ls.get("user_auth")
        if not dc_str or not user_auth_str:
            print(
                f"Error: {LATCHKEY_DUMP_PATH} does not contain valid auth data.",
                file=sys.stderr,
            )
            sys.exit(1)
        dc_id = int(dc_str)
        auth_key_raw = ls.get(f"dc{dc_id}_auth_key", "")
        # The value may be JSON-encoded (wrapped in extra quotes)
        if auth_key_raw.startswith('"'):
            auth_key_hex = json.loads(auth_key_raw)
        else:
            auth_key_hex = auth_key_raw
        if not auth_key_hex:
            print(f"Error: no auth_key for DC {dc_id} in dump.", file=sys.stderr)
            sys.exit(1)
        return _auth_key_to_string_session(dc_id, auth_key_hex)

    print(
        "Error: no Telegram credentials found.\n"
        "Provide credentials via one of:\n"
        "  - TELEGRAM_STRING_SESSION environment variable\n"
        "  - TELEGRAM_DC_ID + TELEGRAM_AUTH_KEY_HEX environment variables\n"
        "  - Run 'latchkey auth browser telegram' to create "
        f"{LATCHKEY_DUMP_PATH}",
        file=sys.stderr,
    )
    sys.exit(1)


async def create_bot(bot_display_name: str, bot_username: str) -> tuple[str, str]:
    """Create a Telegram bot via BotFather.

    Returns (bot_token, bot_username) on success.
    """
    session_str = _get_string_session()
    api_id, api_hash = _fetch_telegram_web_api_credentials()

    client = TelegramClient(StringSession(session_str), api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        print(
            "Error: Telegram session is not authorized. "
            "The auth key may have been revoked.\n"
            "Run 'latchkey auth browser telegram' to log in again.",
            file=sys.stderr,
        )
        await client.disconnect()
        sys.exit(1)

    me = await client.get_me()
    print(f"Connected as: {me.first_name} (id={me.id})", file=sys.stderr)

    botfather = await client.get_entity("@BotFather")

    # Step 1: Send /newbot
    await client.send_message(botfather, "/newbot")
    await asyncio.sleep(2)

    messages = await client.get_messages(botfather, limit=1)
    response_text = messages[0].text if messages else ""
    if "choose a name" not in response_text.lower():
        print(
            f"Error: unexpected BotFather response to /newbot:\n{response_text}",
            file=sys.stderr,
        )
        await client.disconnect()
        sys.exit(1)

    # Step 2: Send the display name
    await client.send_message(botfather, bot_display_name)
    await asyncio.sleep(2)

    messages = await client.get_messages(botfather, limit=1)
    response_text = messages[0].text if messages else ""
    if "username" not in response_text.lower():
        print(
            f"Error: unexpected BotFather response to bot name:\n{response_text}",
            file=sys.stderr,
        )
        await client.disconnect()
        sys.exit(1)

    # Step 3: Send the username
    await client.send_message(botfather, bot_username)
    await asyncio.sleep(2)

    messages = await client.get_messages(botfather, limit=1)
    response_text = messages[0].text if messages else ""

    # Check for error (username taken, invalid, etc.)
    if "sorry" in response_text.lower() or "error" in response_text.lower():
        print(
            f"Error: BotFather rejected the username:\n{response_text}",
            file=sys.stderr,
        )
        await client.disconnect()
        sys.exit(1)

    # Parse the bot token from the response
    # BotFather sends: "Use this token to access the HTTP API:\n<id>:<hash>"
    token_match = re.search(r"(\d+:[A-Za-z0-9_-]+)", response_text)
    if not token_match:
        print(
            f"Error: could not extract bot token from BotFather response:\n{response_text}",
            file=sys.stderr,
        )
        await client.disconnect()
        sys.exit(1)

    bot_token = token_match.group(1)

    # Extract the actual username from the response
    username_match = re.search(r"t\.me/(\w+)", response_text)
    actual_username = username_match.group(1) if username_match else bot_username

    await client.disconnect()
    return bot_token, actual_username


def main() -> None:
    if len(sys.argv) != 3:
        print(
            "Usage: python create_telegram_bot.py <bot_display_name> <bot_username>\n"
            "\n"
            "  bot_display_name: Human-readable name (e.g. 'My Cool Bot')\n"
            "  bot_username: Username ending in 'bot' (e.g. 'my_cool_bot')",
            file=sys.stderr,
        )
        sys.exit(1)

    bot_display_name = sys.argv[1]
    bot_username = sys.argv[2]

    if not bot_username.lower().endswith("bot"):
        print(
            f"Error: bot username must end in 'bot' (got '{bot_username}').\n"
            "Example: my_cool_bot",
            file=sys.stderr,
        )
        sys.exit(1)

    bot_token, actual_username = asyncio.run(
        create_bot(bot_display_name, bot_username)
    )

    # Print both values to stdout (all other output goes to stderr)
    print(f"bot_token={bot_token}")
    print(f"bot_username={actual_username}")


if __name__ == "__main__":
    main()
