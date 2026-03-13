from imbue.minds.forwarding_server.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.forwarding_server.cookie_manager import create_session_cookie
from imbue.minds.forwarding_server.cookie_manager import verify_session_cookie
from imbue.minds.primitives import CookieSigningKey


def test_session_cookie_name_is_stable() -> None:
    assert SESSION_COOKIE_NAME == "mind_session"


def test_create_and_verify_session_cookie_round_trip() -> None:
    key = CookieSigningKey("test-secret-key-83742")

    cookie_value = create_session_cookie(signing_key=key)
    is_valid = verify_session_cookie(cookie_value=cookie_value, signing_key=key)

    assert is_valid is True


def test_verify_session_cookie_returns_false_for_wrong_key() -> None:
    correct_key = CookieSigningKey("correct-key-19283")
    wrong_key = CookieSigningKey("wrong-key-84729")

    cookie_value = create_session_cookie(signing_key=correct_key)
    result = verify_session_cookie(cookie_value=cookie_value, signing_key=wrong_key)

    assert result is False


def test_verify_session_cookie_returns_false_for_tampered_value() -> None:
    key = CookieSigningKey("test-key-38472")
    result = verify_session_cookie(
        cookie_value="tampered-garbage-value",
        signing_key=key,
    )
    assert result is False


def test_verify_session_cookie_returns_false_for_empty_value() -> None:
    key = CookieSigningKey("test-key-19384")
    result = verify_session_cookie(
        cookie_value="",
        signing_key=key,
    )
    assert result is False
