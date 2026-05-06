from imbue.mngr_forward.cookie import create_session_cookie
from imbue.mngr_forward.cookie import create_subdomain_auth_token
from imbue.mngr_forward.cookie import verify_session_cookie
from imbue.mngr_forward.cookie import verify_subdomain_auth_token
from imbue.mngr_forward.primitives import CookieSigningKey


def test_session_cookie_round_trip() -> None:
    key = CookieSigningKey("test-secret-key-1234567890")
    cookie = create_session_cookie(key)
    assert verify_session_cookie(cookie_value=cookie, signing_key=key) is True


def test_session_cookie_with_wrong_key_fails() -> None:
    cookie = create_session_cookie(CookieSigningKey("a"))
    assert verify_session_cookie(cookie_value=cookie, signing_key=CookieSigningKey("b")) is False


def test_session_cookie_tampered_payload_fails() -> None:
    key = CookieSigningKey("test-key")
    cookie = create_session_cookie(key)
    tampered = cookie[:-3] + "xyz"
    assert verify_session_cookie(cookie_value=tampered, signing_key=key) is False


def test_session_cookie_preauth_short_circuit() -> None:
    key = CookieSigningKey("test-key")
    assert (
        verify_session_cookie(
            cookie_value="opaque-token",
            signing_key=key,
            preauth_cookie_value="opaque-token",
        )
        is True
    )


def test_session_cookie_preauth_mismatch_falls_back_to_signature() -> None:
    key = CookieSigningKey("test-key")
    assert (
        verify_session_cookie(
            cookie_value="not-the-preauth",
            signing_key=key,
            preauth_cookie_value="opaque-token",
        )
        is False
    )


def test_subdomain_auth_token_round_trip() -> None:
    key = CookieSigningKey("test-key")
    token = create_subdomain_auth_token(signing_key=key, agent_id="agent-abc")
    assert verify_subdomain_auth_token(token=token, signing_key=key, agent_id="agent-abc") is True


def test_subdomain_auth_token_audience_binding() -> None:
    key = CookieSigningKey("test-key")
    token = create_subdomain_auth_token(signing_key=key, agent_id="agent-abc")
    assert verify_subdomain_auth_token(token=token, signing_key=key, agent_id="agent-other") is False
