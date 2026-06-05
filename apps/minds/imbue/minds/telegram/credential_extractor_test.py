"""Unit tests for the pure localStorage-parsing helpers in credential_extractor.

The live-browser entry point (``extract_telegram_credentials_from_browser``)
requires Playwright and a real Telegram login, so it is not unit-tested here.
Instead, all parsing/validation logic is factored into pure helpers that take the
raw localStorage strings, and those are exercised exhaustively below.
"""

import json

import pytest

from imbue.minds.errors import TelegramCredentialExtractionError
from imbue.minds.telegram.credential_extractor import _AUTH_KEY_HEX_LENGTH
from imbue.minds.telegram.credential_extractor import _parse_auth_key_hex
from imbue.minds.telegram.credential_extractor import _parse_dc_id_and_user_id
from imbue.minds.telegram.credential_extractor import _parse_first_name


def test_parse_dc_id_and_user_id_extracts_both_from_valid_data() -> None:
    dc_id, user_id = _parse_dc_id_and_user_id("2", json.dumps({"id": 12345}))
    assert dc_id == 2
    assert user_id == "12345"


def test_parse_dc_id_and_user_id_raises_when_dc_missing() -> None:
    with pytest.raises(TelegramCredentialExtractionError, match="Could not find Telegram auth data"):
        _parse_dc_id_and_user_id(None, json.dumps({"id": 1}))


def test_parse_dc_id_and_user_id_raises_when_user_auth_missing() -> None:
    with pytest.raises(TelegramCredentialExtractionError, match="Could not find Telegram auth data"):
        _parse_dc_id_and_user_id("2", None)


def test_parse_dc_id_and_user_id_raises_when_dc_missing_and_empty_string() -> None:
    # Empty strings are falsy and must be treated the same as None.
    with pytest.raises(TelegramCredentialExtractionError, match="Could not find Telegram auth data"):
        _parse_dc_id_and_user_id("", json.dumps({"id": 1}))


def test_parse_dc_id_and_user_id_raises_on_non_integer_dc() -> None:
    with pytest.raises(TelegramCredentialExtractionError, match="Invalid data center ID in localStorage: 'abc'"):
        _parse_dc_id_and_user_id("abc", json.dumps({"id": 1}))


def test_parse_dc_id_and_user_id_raises_on_unparseable_user_auth() -> None:
    with pytest.raises(TelegramCredentialExtractionError, match="Could not parse user_auth from localStorage"):
        _parse_dc_id_and_user_id("2", "not valid json {{{")


def test_parse_dc_id_and_user_id_raises_when_user_id_absent() -> None:
    with pytest.raises(TelegramCredentialExtractionError, match="does not contain a user ID"):
        _parse_dc_id_and_user_id("2", json.dumps({"first_name": "Alice"}))


def test_parse_auth_key_hex_accepts_bare_hex_string() -> None:
    bare = "ab" * 256
    assert _parse_auth_key_hex(bare, dc_id=2) == bare


def test_parse_auth_key_hex_unwraps_json_quoted_value() -> None:
    bare = "cd" * 256
    json_wrapped = json.dumps(bare)  # adds surrounding double quotes
    assert json_wrapped.startswith('"')
    # The bare and JSON-wrapped forms must normalize to the exact same hex.
    assert _parse_auth_key_hex(json_wrapped, dc_id=2) == bare


def test_parse_auth_key_hex_raises_when_missing() -> None:
    with pytest.raises(TelegramCredentialExtractionError, match=r"Could not find auth key for DC 3 .*dc3_auth_key"):
        _parse_auth_key_hex(None, dc_id=3)


def test_parse_auth_key_hex_raises_on_unparseable_json_quoted_value() -> None:
    with pytest.raises(TelegramCredentialExtractionError, match="Could not parse auth_key for DC 2"):
        _parse_auth_key_hex('"unterminated', dc_id=2)


def test_parse_auth_key_hex_raises_on_wrong_length() -> None:
    too_short = "ab" * 100  # 200 hex chars, not 512
    with pytest.raises(TelegramCredentialExtractionError, match="unexpected length: 200 hex chars"):
        _parse_auth_key_hex(too_short, dc_id=2)


def test_parse_auth_key_hex_validates_against_declared_length_constant() -> None:
    # A value of exactly _AUTH_KEY_HEX_LENGTH must pass; one char short must fail.
    valid = "f" * _AUTH_KEY_HEX_LENGTH
    assert _parse_auth_key_hex(valid, dc_id=1) == valid
    with pytest.raises(TelegramCredentialExtractionError, match="unexpected length"):
        _parse_auth_key_hex("f" * (_AUTH_KEY_HEX_LENGTH - 1), dc_id=1)


def test_parse_first_name_extracts_first_name_from_account_data() -> None:
    assert _parse_first_name(json.dumps({"firstName": "Alice", "lastName": "Smith"})) == "Alice"


def test_parse_first_name_returns_empty_string_when_account_data_missing() -> None:
    assert _parse_first_name(None) == ""


def test_parse_first_name_returns_empty_string_when_first_name_absent() -> None:
    assert _parse_first_name(json.dumps({"lastName": "Smith"})) == ""


def test_parse_first_name_returns_empty_string_on_malformed_account_data() -> None:
    # Malformed account data is best-effort and must never raise.
    assert _parse_first_name("not valid json {{{") == ""
