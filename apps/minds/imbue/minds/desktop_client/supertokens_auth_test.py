from pathlib import Path

from imbue.minds.desktop_client.supertokens_auth import SuperTokensSessionStore
from imbue.minds.desktop_client.supertokens_auth import derive_user_id_prefix


def _make_store(tmp_path: Path) -> SuperTokensSessionStore:
    return SuperTokensSessionStore(data_directory=tmp_path / "supertokens")


def test_is_signed_in_false_when_no_session(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.is_signed_in() is False


def test_has_signed_in_before_false_initially(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.has_signed_in_before() is False


def test_store_and_load_session(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.store_session(
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        user_id="a1b2c3d4-e5f6-7890-abcd-1234567890ab",
        email="test@example.com",
        display_name="Test User",
    )

    assert store.is_signed_in() is True
    assert store.has_signed_in_before() is True

    session = store.load_session()
    assert session is not None
    assert str(session.access_token) == "test-access-token"
    assert str(session.refresh_token) == "test-refresh-token"
    assert str(session.user_id) == "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    assert session.email == "test@example.com"
    assert session.display_name == "Test User"


def test_get_access_token(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.store_session(
        access_token="my-jwt-token",
        refresh_token="my-refresh",
        user_id="a1b2c3d4-e5f6-7890-abcd-1234567890ab",
        email="test@example.com",
    )
    assert store.get_access_token() == "my-jwt-token"


def test_get_access_token_none_when_not_signed_in(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.get_access_token() is None


def test_get_user_info(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.store_session(
        access_token="token",
        refresh_token="refresh",
        user_id="a1b2c3d4-e5f6-7890-abcd-1234567890ab",
        email="user@example.com",
        display_name="User Name",
    )
    info = store.get_user_info()
    assert info is not None
    assert info.email == "user@example.com"
    assert info.display_name == "User Name"
    assert str(info.user_id_prefix) == "a1b2c3d4e5f67890"


def test_get_user_info_none_when_not_signed_in(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    assert store.get_user_info() is None


def test_clear_session(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.store_session(
        access_token="token",
        refresh_token="refresh",
        user_id="a1b2c3d4-e5f6-7890-abcd-1234567890ab",
        email="test@example.com",
    )
    assert store.is_signed_in() is True

    store.clear_session()
    assert store.is_signed_in() is False
    assert store.get_access_token() is None
    # has_signed_in_before flag should persist after sign-out
    assert store.has_signed_in_before() is True


def test_update_access_token(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.store_session(
        access_token="old-token",
        refresh_token="refresh",
        user_id="a1b2c3d4-e5f6-7890-abcd-1234567890ab",
        email="test@example.com",
    )
    store.update_access_token("new-token")

    assert store.get_access_token() == "new-token"
    session = store.load_session()
    assert session is not None
    assert str(session.refresh_token) == "refresh"
    assert session.email == "test@example.com"


def test_update_access_token_noop_when_not_signed_in(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.update_access_token("new-token")
    assert store.get_access_token() is None


def test_session_persists_across_instances(tmp_path: Path) -> None:
    store_dir = tmp_path / "supertokens"
    store_a = SuperTokensSessionStore(data_directory=store_dir)
    store_a.store_session(
        access_token="persisted-token",
        refresh_token="persisted-refresh",
        user_id="a1b2c3d4-e5f6-7890-abcd-1234567890ab",
        email="persist@example.com",
    )

    store_b = SuperTokensSessionStore(data_directory=store_dir)
    assert store_b.is_signed_in() is True
    assert store_b.get_access_token() == "persisted-token"


def test_load_session_returns_none_for_corrupt_file(tmp_path: Path) -> None:
    store_dir = tmp_path / "supertokens"
    store_dir.mkdir(parents=True)
    (store_dir / "supertokens_session.json").write_text("not valid json {{{")

    store = SuperTokensSessionStore(data_directory=store_dir)
    assert store.load_session() is None


def test_session_file_has_restricted_permissions(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.store_session(
        access_token="token",
        refresh_token="refresh",
        user_id="a1b2c3d4-e5f6-7890-abcd-1234567890ab",
        email="test@example.com",
    )

    session_path = tmp_path / "supertokens" / "supertokens_session.json"
    permissions = session_path.stat().st_mode & 0o777
    assert permissions == 0o600


def test_derive_user_id_prefix_strips_hyphens_and_truncates() -> None:
    user_id = "a1b2c3d4-e5f6-7890-abcd-1234567890ab"
    prefix = derive_user_id_prefix(user_id)
    assert str(prefix) == "a1b2c3d4e5f67890"
    assert len(str(prefix)) == 16


def test_derive_user_id_prefix_handles_short_ids() -> None:
    prefix = derive_user_id_prefix("abcd")
    assert str(prefix) == "abcd"


def test_store_session_without_display_name(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.store_session(
        access_token="token",
        refresh_token="refresh",
        user_id="a1b2c3d4-e5f6-7890-abcd-1234567890ab",
        email="test@example.com",
    )
    session = store.load_session()
    assert session is not None
    assert session.display_name is None
