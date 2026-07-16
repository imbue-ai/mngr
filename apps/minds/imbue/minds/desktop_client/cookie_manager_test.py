from flask import Response

from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import clear_session_cookie
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.primitives import CookieSigningKey


def test_session_cookie_name_is_stable() -> None:
    assert SESSION_COOKIE_NAME == "minds_session"


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


def test_clear_session_cookie_emits_expired_set_cookie() -> None:
    response = Response()

    clear_session_cookie(response)

    set_cookie_headers = response.headers.getlist("Set-Cookie")
    session_headers = [h for h in set_cookie_headers if h.startswith(f"{SESSION_COOKIE_NAME}=")]
    assert len(session_headers) == 1
    header = session_headers[0]
    # An empty value plus a past expiry is how a browser is told to drop the
    # cookie; werkzeug also emits ``Max-Age=0``.
    assert f"{SESSION_COOKIE_NAME}=;" in header
    assert "Expires=Thu, 01 Jan 1970" in header
    assert "Max-Age=0" in header
    assert "Path=/" in header
