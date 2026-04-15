from pathlib import Path

from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.session_store import derive_user_id_prefix


def _make_store(tmp_path: Path) -> MultiAccountSessionStore:
    return MultiAccountSessionStore(data_dir=tmp_path)


def _add_user(store: MultiAccountSessionStore, user_id: str = "user-1", email: str = "a@b.com") -> None:
    store.add_or_update_session(
        access_token="tok-" + user_id,
        refresh_token=None,
        user_id=user_id,
        email=email,
    )


def test_add_and_load_session(tmp_path: Path) -> None:
    """Adding a session persists it to disk and can be loaded back."""
    store = _make_store(tmp_path)
    _add_user(store, "user-aaa", "aaa@example.com")

    loaded = store.get_session("user-aaa")
    assert loaded is not None
    assert loaded.email == "aaa@example.com"
    assert str(loaded.access_token) == "tok-user-aaa"


def test_add_multiple_accounts(tmp_path: Path) -> None:
    """Multiple accounts can coexist in the same store."""
    store = _make_store(tmp_path)
    _add_user(store, "user-1", "one@example.com")
    _add_user(store, "user-2", "two@example.com")

    accounts = store.list_accounts()
    assert len(accounts) == 2
    emails = {a.email for a in accounts}
    assert emails == {"one@example.com", "two@example.com"}


def test_update_existing_session_preserves_workspaces(tmp_path: Path) -> None:
    """Updating tokens for an existing user preserves workspace associations."""
    store = _make_store(tmp_path)
    _add_user(store, "user-1", "a@b.com")
    store.associate_workspace("user-1", "agent-xyz")

    # Re-add with new tokens
    store.add_or_update_session(
        access_token="new-tok",
        refresh_token=None,
        user_id="user-1",
        email="a@b.com",
    )

    session = store.get_session("user-1")
    assert session is not None
    assert session.workspace_ids == ["agent-xyz"]
    assert str(session.access_token) == "new-tok"


def test_remove_session(tmp_path: Path) -> None:
    """Removing a session deletes it from the store."""
    store = _make_store(tmp_path)
    _add_user(store, "user-1")
    store.remove_session("user-1")

    assert store.get_session("user-1") is None
    assert store.list_accounts() == []


def test_associate_and_disassociate_workspace(tmp_path: Path) -> None:
    """Workspace association and disassociation work correctly."""
    store = _make_store(tmp_path)
    _add_user(store, "user-1")

    store.associate_workspace("user-1", "agent-aaa")
    store.associate_workspace("user-1", "agent-bbb")

    session = store.get_session("user-1")
    assert session is not None
    assert session.workspace_ids == ["agent-aaa", "agent-bbb"]

    store.disassociate_workspace("user-1", "agent-aaa")
    session = store.get_session("user-1")
    assert session is not None
    assert session.workspace_ids == ["agent-bbb"]


def test_get_account_for_workspace(tmp_path: Path) -> None:
    """Can look up which account a workspace belongs to."""
    store = _make_store(tmp_path)
    _add_user(store, "user-1", "one@example.com")
    _add_user(store, "user-2", "two@example.com")
    store.associate_workspace("user-1", "agent-aaa")
    store.associate_workspace("user-2", "agent-bbb")

    account = store.get_account_for_workspace("agent-aaa")
    assert account is not None
    assert account.email == "one@example.com"

    account = store.get_account_for_workspace("agent-bbb")
    assert account is not None
    assert account.email == "two@example.com"

    assert store.get_account_for_workspace("agent-unknown") is None


def test_duplicate_associate_is_idempotent(tmp_path: Path) -> None:
    """Associating the same workspace twice doesn't create duplicates."""
    store = _make_store(tmp_path)
    _add_user(store, "user-1")
    store.associate_workspace("user-1", "agent-aaa")
    store.associate_workspace("user-1", "agent-aaa")

    session = store.get_session("user-1")
    assert session is not None
    assert session.workspace_ids == ["agent-aaa"]


def test_get_user_info(tmp_path: Path) -> None:
    """get_user_info returns a UserInfo with derived prefix."""
    store = _make_store(tmp_path)
    store.add_or_update_session(
        access_token="tok",
        refresh_token=None,
        user_id="abcd1234-5678-9abc-def0-1234567890ab",
        email="test@example.com",
        display_name="Test User",
    )

    info = store.get_user_info("abcd1234-5678-9abc-def0-1234567890ab")
    assert info is not None
    assert info.email == "test@example.com"
    assert info.display_name == "Test User"
    assert str(info.user_id_prefix) == "abcd123456789abc"


def test_corrupt_file_returns_empty(tmp_path: Path) -> None:
    """A corrupt sessions.json is handled gracefully."""
    store = _make_store(tmp_path)
    (tmp_path / "sessions.json").write_text("not valid json {{{")
    assert store.list_accounts() == []


def test_is_any_signed_in(tmp_path: Path) -> None:
    """is_any_signed_in reflects session presence."""
    store = _make_store(tmp_path)
    assert not store.is_any_signed_in()
    _add_user(store, "user-1")
    assert store.is_any_signed_in()


def test_derive_user_id_prefix() -> None:
    """derive_user_id_prefix strips hyphens and takes first 16 chars."""
    prefix = derive_user_id_prefix("abcd1234-5678-9abc-def0-1234567890ab")
    assert str(prefix) == "abcd123456789abc"


def test_persistence_across_store_instances(tmp_path: Path) -> None:
    """Data written by one store instance is readable by a new one."""
    store1 = _make_store(tmp_path)
    _add_user(store1, "user-1", "persist@test.com")
    store1.associate_workspace("user-1", "agent-xyz")

    store2 = _make_store(tmp_path)
    session = store2.get_session("user-1")
    assert session is not None
    assert session.email == "persist@test.com"
    assert session.workspace_ids == ["agent-xyz"]
