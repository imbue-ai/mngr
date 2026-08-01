from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.mngr_imbue_cloud.connector.session_store import ImbueCloudSessionStore
from imbue.mngr_imbue_cloud.connector.session_store import _decode_jwt_exp
from imbue.mngr_imbue_cloud.connector.session_store import make_session_from_tokens
from imbue.mngr_imbue_cloud.data_types import AuthSession
from imbue.mngr_imbue_cloud.errors import ImbueCloudAuthError
from imbue.mngr_imbue_cloud.primitives import ImbueCloudAccount
from imbue.mngr_imbue_cloud.primitives import SuperTokensUserId


def _make_session(
    user_id: str = "user-abc",
    email: str = "alice@imbue.com",
    access_token: str = "header.payload.sig",
    refresh_token: str | None = "refresh-tok",
    access_token_expires_at: datetime | None = None,
    is_pending_verification: bool = False,
) -> AuthSession:
    return AuthSession(
        user_id=SuperTokensUserId(user_id),
        email=ImbueCloudAccount(email),
        display_name=None,
        access_token=SecretStr(access_token),
        refresh_token=SecretStr(refresh_token) if refresh_token else None,
        access_token_expires_at=access_token_expires_at,
        is_pending_verification=is_pending_verification,
    )


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    session = _make_session()
    store.save(session)

    loaded = store.load_by_account(ImbueCloudAccount("alice@imbue.com"))
    assert loaded is not None
    assert loaded.user_id == session.user_id
    assert loaded.access_token.get_secret_value() == "header.payload.sig"


def test_load_by_account_returns_none_when_missing(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    assert store.load_by_account(ImbueCloudAccount("nobody@example.com")) is None


def test_delete_by_account_clears_session_and_index(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    store.save(_make_session())
    store.delete_by_account(ImbueCloudAccount("alice@imbue.com"))
    assert store.load_by_account(ImbueCloudAccount("alice@imbue.com")) is None
    # Idempotent: deleting again does not raise.
    store.delete_by_account(ImbueCloudAccount("alice@imbue.com"))


def test_list_accounts_returns_all_signed_in_users(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    store.save(_make_session(user_id="user-a", email="alice@imbue.com"))
    store.save(_make_session(user_id="user-b", email="bob@imbue.com"))
    accounts = store.list_accounts()
    assert set(accounts) == {ImbueCloudAccount("alice@imbue.com"), ImbueCloudAccount("bob@imbue.com")}


def test_pending_verification_flag_survives_save_and_load(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    store.save(_make_session(is_pending_verification=True))
    loaded = store.load_by_account(ImbueCloudAccount("alice@imbue.com"))
    assert loaded is not None
    assert loaded.is_pending_verification is True


def test_promote_pending_session_clears_flag_on_disk(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    store.save(_make_session(is_pending_verification=True))
    promoted = store.promote_pending_session(ImbueCloudAccount("alice@imbue.com"))
    assert promoted is not None
    assert promoted.is_pending_verification is False
    reloaded = store.load_by_account(ImbueCloudAccount("alice@imbue.com"))
    assert reloaded is not None
    assert reloaded.is_pending_verification is False
    # The tokens survive promotion unchanged.
    assert reloaded.access_token.get_secret_value() == "header.payload.sig"


def test_promote_pending_session_is_idempotent_and_handles_missing(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    assert store.promote_pending_session(ImbueCloudAccount("nobody@example.com")) is None
    store.save(_make_session(is_pending_verification=False))
    promoted = store.promote_pending_session(ImbueCloudAccount("alice@imbue.com"))
    assert promoted is not None
    assert promoted.is_pending_verification is False


def test_set_active_account_refuses_pending_session(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    store.save(_make_session(is_pending_verification=True))
    with pytest.raises(ImbueCloudAuthError, match="not verified"):
        store.set_active_account(ImbueCloudAccount("alice@imbue.com"))
    assert store.get_active_account() is None
    # After promotion the same call succeeds.
    store.promote_pending_session(ImbueCloudAccount("alice@imbue.com"))
    store.set_active_account(ImbueCloudAccount("alice@imbue.com"))
    assert store.get_active_account() == ImbueCloudAccount("alice@imbue.com")


def test_is_access_token_near_expiry_returns_true_when_unknown(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    session = _make_session(access_token_expires_at=None)
    assert store.is_access_token_near_expiry(session)


def test_is_access_token_near_expiry_respects_buffer(tmp_path: Path) -> None:
    store = ImbueCloudSessionStore(sessions_dir=tmp_path)
    far_future = datetime.now(timezone.utc) + timedelta(hours=1)
    not_near = _make_session(access_token_expires_at=far_future)
    assert not store.is_access_token_near_expiry(not_near)

    near_now = datetime.now(timezone.utc) + timedelta(seconds=10)
    near = _make_session(access_token_expires_at=near_now)
    assert store.is_access_token_near_expiry(near)


def test_decode_jwt_exp_returns_none_for_garbage() -> None:
    assert _decode_jwt_exp("not-a-jwt") is None
    assert _decode_jwt_exp("a.b.c") is None  # invalid base64 payload


def test_make_session_from_tokens_extracts_exp() -> None:
    # JWT with payload {"exp": 9999999999, "sub": "x"}, base64url encoded.
    jwt_with_exp = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjk5OTk5OTk5OTksInN1YiI6IngifQ.sig"
    session = make_session_from_tokens(
        user_id=SuperTokensUserId("u1"),
        email=ImbueCloudAccount("a@b.com"),
        display_name=None,
        access_token=jwt_with_exp,
        refresh_token=None,
        is_pending_verification=False,
    )
    assert session.access_token_expires_at is not None
    assert session.access_token_expires_at.year >= 2286
