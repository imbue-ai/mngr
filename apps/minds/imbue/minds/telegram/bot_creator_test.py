import base64

import pytest
from inline_snapshot import snapshot

from imbue.minds.errors import TelegramCredentialError
from imbue.minds.telegram.bot_creator import DC_IPS
from imbue.minds.telegram.bot_creator import _BOT_TOKEN_PATTERN
from imbue.minds.telegram.bot_creator import _BOT_USERNAME_PATTERN
from imbue.minds.telegram.bot_creator import _FALLBACK_API_HASH
from imbue.minds.telegram.bot_creator import _FALLBACK_API_ID
from imbue.minds.telegram.bot_creator import auth_key_to_string_session


def test_auth_key_to_string_session_produces_valid_session_string() -> None:
    """Verify that a known dc_id + auth_key produces a deterministic session string."""
    dc_id = 2
    auth_key_hex = "00" * 256
    result = auth_key_to_string_session(dc_id, auth_key_hex)

    # Should start with "1" (version prefix)
    assert result.startswith("1")

    # Should be a valid base64-encoded string (after stripping version prefix)
    decoded = base64.urlsafe_b64decode(result[1:])

    # Format: dc_id (1 byte) + ip (4 bytes) + port (2 bytes) + key (256 bytes)
    assert len(decoded) == snapshot(263)
    assert decoded[0] == dc_id


def test_auth_key_to_string_session_rejects_unknown_dc_id() -> None:
    with pytest.raises(TelegramCredentialError, match="Unknown Telegram data center ID: 99"):
        auth_key_to_string_session(99, "00" * 256)


def test_auth_key_to_string_session_rejects_invalid_hex() -> None:
    with pytest.raises(TelegramCredentialError, match="auth_key is not valid hex"):
        auth_key_to_string_session(1, "not_hex")


def test_auth_key_to_string_session_rejects_wrong_length() -> None:
    with pytest.raises(TelegramCredentialError, match="auth_key must be 256 bytes"):
        auth_key_to_string_session(1, "00" * 128)


def test_dc_ips_covers_all_five_data_centers() -> None:
    assert set(DC_IPS.keys()) == snapshot({1, 2, 3, 4, 5})


def test_auth_key_to_string_session_works_for_all_dc_ids() -> None:
    auth_key_hex = "ff" * 256
    for dc_id in DC_IPS:
        result = auth_key_to_string_session(dc_id, auth_key_hex)
        assert result.startswith("1")


def test_auth_key_to_string_session_encodes_correct_dc_and_key() -> None:
    """Verify the packed binary contains the correct dc_id and auth key bytes."""
    dc_id = 1
    auth_key_hex = "ab" * 256
    result = auth_key_to_string_session(dc_id, auth_key_hex)

    decoded = base64.urlsafe_b64decode(result[1:])

    # First byte is dc_id
    assert decoded[0] == 1

    # Last 256 bytes are the auth key
    auth_key_bytes = decoded[-256:]
    assert auth_key_bytes == bytes.fromhex(auth_key_hex)


def test_bot_token_pattern_matches_valid_tokens() -> None:
    assert _BOT_TOKEN_PATTERN.search("Use this token: 123456:ABC-def_GHI")
    assert _BOT_TOKEN_PATTERN.search("123:abc") is not None
    assert _BOT_TOKEN_PATTERN.search("no token here") is None


def test_bot_username_pattern_matches_tme_links() -> None:
    match = _BOT_USERNAME_PATTERN.search("Go to t.me/my_cool_bot to start")
    assert match is not None
    assert match.group(1) == "my_cool_bot"

    assert _BOT_USERNAME_PATTERN.search("no link here") is None


def test_fallback_api_credentials_are_valid() -> None:
    assert _FALLBACK_API_ID > 0
    assert len(_FALLBACK_API_HASH) == 32
