from pathlib import Path

import pytest
from itsdangerous import TimestampSigner
from itsdangerous import URLSafeTimedSerializer

from imbue.mngr_forward.auth import FileAuthStore
from imbue.mngr_forward.cookie import _COOKIE_MAX_AGE_SECONDS
from imbue.mngr_forward.cookie import _COOKIE_SALT
from imbue.mngr_forward.cookie import _SESSION_PAYLOAD
from imbue.mngr_forward.cookie import create_session_cookie
from imbue.mngr_forward.cookie import create_subdomain_auth_token
from imbue.mngr_forward.cookie import verify_session_cookie
from imbue.mngr_forward.cookie import verify_subdomain_auth_token
from imbue.mngr_forward.primitives import CookieSigningKey


@pytest.mark.witnesses(
    "sessions-unforgeable", partial="the 30-day bound; the other accepted/rejected states are witnessed separately"
)
def test_session_cookie_older_than_max_age_fails() -> None:
    """A validly-signed cookie whose timestamp is older than 30 days is rejected.

    The cookie is signed with the real key so its signature verifies -- only its
    embedded timestamp is aged, isolating the max-age bound from tampering. A
    custom ``TimestampSigner`` backdates the timestamp; nothing else about the
    token differs from one ``create_session_cookie`` would mint.
    """
    key = CookieSigningKey("test-secret-key-1234567890")

    class _PastTimestampSigner(TimestampSigner):
        def get_timestamp(self) -> int:
            return super().get_timestamp() - (_COOKIE_MAX_AGE_SECONDS + 24 * 60 * 60)

    aged_serializer = URLSafeTimedSerializer(secret_key=key.get_secret_value(), signer=_PastTimestampSigner)
    aged_cookie = aged_serializer.dumps(_SESSION_PAYLOAD, salt=_COOKIE_SALT)
    assert verify_session_cookie(cookie_value=aged_cookie, signing_key=key) is False


@pytest.mark.witnesses(
    "sessions-unforgeable",
    partial="the different-state-directory clause; the other accepted/rejected states are witnessed separately",
)
def test_session_cookie_from_different_state_directory_fails(tmp_path: Path) -> None:
    """A cookie signed under one state directory's key does not verify under another's.

    Each ``FileAuthStore`` mints its signing key inside its own data directory,
    so a session cookie issued against store A's key is valid there but invalid
    at store B -- the "signed under a different state directory" clause.
    """
    store_a = FileAuthStore(data_directory=tmp_path / "state_a")
    store_b = FileAuthStore(data_directory=tmp_path / "state_b")
    cookie_from_a = create_session_cookie(store_a.get_signing_key())
    assert verify_session_cookie(cookie_value=cookie_from_a, signing_key=store_a.get_signing_key()) is True
    assert verify_session_cookie(cookie_value=cookie_from_a, signing_key=store_b.get_signing_key()) is False


@pytest.mark.witnesses(
    "sessions-unforgeable", partial="one representative valid cookie is accepted; not every accepted state"
)
def test_session_cookie_round_trip() -> None:
    key = CookieSigningKey("test-secret-key-1234567890")
    cookie = create_session_cookie(key)
    assert verify_session_cookie(cookie_value=cookie, signing_key=key) is True


@pytest.mark.witnesses(
    "sessions-unforgeable", partial="one wrong key; the state-directory framing of this clause is witnessed separately"
)
def test_session_cookie_with_wrong_key_fails() -> None:
    cookie = create_session_cookie(CookieSigningKey("a"))
    assert verify_session_cookie(cookie_value=cookie, signing_key=CookieSigningKey("b")) is False


@pytest.mark.witnesses(
    "sessions-unforgeable", partial="one representative alteration; the 'any alteration' quantifier stays open"
)
def test_session_cookie_tampered_payload_fails() -> None:
    key = CookieSigningKey("test-key")
    cookie = create_session_cookie(key)
    tampered = cookie[:-3] + "xyz"
    assert verify_session_cookie(cookie_value=tampered, signing_key=key) is False


@pytest.mark.witnesses(
    "sessions-unforgeable", partial="the exact-preauth-value accepted path; not every accepted state"
)
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
    token = create_subdomain_auth_token(signing_key=key, origin_coordinate="host-abc")
    assert verify_subdomain_auth_token(token=token, signing_key=key, origin_coordinate="host-abc") is True


def test_subdomain_auth_token_audience_binding() -> None:
    key = CookieSigningKey("test-key")
    token = create_subdomain_auth_token(signing_key=key, origin_coordinate="host-abc")
    assert verify_subdomain_auth_token(token=token, signing_key=key, origin_coordinate="host-other") is False
