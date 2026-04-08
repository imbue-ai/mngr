"""Unit tests for credential_extractor constants and module-level attributes."""

from imbue.minds.telegram.credential_extractor import _AUTH_KEY_HEX_LENGTH
from imbue.minds.telegram.data_types import TELEGRAM_WEB_URL
from imbue.minds.telegram.credential_extractor import _DEFAULT_LOGIN_TIMEOUT_SECONDS


def test_telegram_web_url_points_to_web_a() -> None:
    assert TELEGRAM_WEB_URL == "https://web.telegram.org/a/"


def test_auth_key_hex_length_is_512() -> None:
    assert _AUTH_KEY_HEX_LENGTH == 512


def test_default_login_timeout_is_five_minutes() -> None:
    assert _DEFAULT_LOGIN_TIMEOUT_SECONDS == 300
