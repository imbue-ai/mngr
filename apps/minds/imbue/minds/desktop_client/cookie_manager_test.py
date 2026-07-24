import pytest

from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.cookie_manager import verify_session_cookie
from imbue.minds.desktop_client.testing import make_backdated_session_cookie
from imbue.minds.primitives import CookieSigningKey


def test_session_cookie_name_is_stable() -> None:
    assert SESSION_COOKIE_NAME == "minds_session"


@pytest.mark.witnesses(
    "authentication.sessions-unforgeable",
    partial="unit-level positive case of 'only tokens issued by this installation are accepted'; not the HTTP surface",
)
def test_create_and_verify_session_cookie_round_trip() -> None:
    key = CookieSigningKey("test-secret-key-83742")

    cookie_value = create_session_cookie(signing_key=key)
    is_valid = verify_session_cookie(cookie_value=cookie_value, signing_key=key)

    assert is_valid is True


@pytest.mark.witnesses(
    "authentication.foreign-token",
    partial="unit-level: verification under a different key fails; does not use two data directories or exercise the HTTP signed-out treatment",
)
@pytest.mark.witnesses(
    "authentication.sessions-unforgeable",
    partial="unit-level: the 'tokens from another installation are invalid' clause via the predicate, not at the HTTP surface",
)
def test_verify_session_cookie_returns_false_for_wrong_key() -> None:
    correct_key = CookieSigningKey("correct-key-19283")
    wrong_key = CookieSigningKey("wrong-key-84729")

    cookie_value = create_session_cookie(signing_key=correct_key)
    result = verify_session_cookie(cookie_value=cookie_value, signing_key=wrong_key)

    assert result is False


@pytest.mark.witnesses(
    "authentication.tampered-token",
    partial="unit-level: the cookie predicate rejects a non-verifiable value (garbage, not a mutated valid token); not the HTTP signed-out treatment",
)
@pytest.mark.witnesses(
    "authentication.sessions-unforgeable",
    partial="unit-level: the 'any alteration invalidates' clause via the predicate, not at the HTTP surface",
)
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


@pytest.mark.witnesses(
    "authentication.sessions-unforgeable",
    partial="unit-level: the 'tokens older than 30 days are invalid' clause via the predicate, not at the HTTP surface",
)
def test_verify_session_cookie_rejects_a_token_older_than_the_max_age() -> None:
    key = CookieSigningKey("test-key-expiry-40192")
    # A just-minted cookie from the same construction verifies, isolating age as
    # the reason the backdated one below is rejected.
    assert verify_session_cookie(cookie_value=make_backdated_session_cookie(key, age_seconds=0), signing_key=key) is True

    expired = make_backdated_session_cookie(key, age_seconds=31 * 24 * 60 * 60)
    assert verify_session_cookie(cookie_value=expired, signing_key=key) is False
