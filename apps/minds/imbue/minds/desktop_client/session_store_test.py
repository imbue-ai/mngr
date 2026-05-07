from pathlib import Path

from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.session_store import derive_user_id_prefix


def _make_store_with_users(
    tmp_path: Path,
    users: list[tuple[str, str, str | None]] | None = None,
) -> tuple[MultiAccountSessionStore, FakeImbueCloudCli]:
    """Build a store seeded with the given (user_id, email, display_name) tuples."""
    cli = make_fake_imbue_cloud_cli()
    for user_id, email, display_name in users or []:
        cli.add_account(user_id=user_id, email=email, display_name=display_name)
    store = make_session_store_for_test(tmp_path, cli=cli)
    return store, cli


def test_add_and_load_session(tmp_path: Path) -> None:
    """A signed-in user is reachable via get_session(user_id)."""
    store, _cli = _make_store_with_users(tmp_path, [("user-aaa", "aaa@example.com", None)])

    loaded = store.get_session("user-aaa")
    assert loaded is not None
    assert loaded.email == "aaa@example.com"


def test_add_multiple_accounts(tmp_path: Path) -> None:
    """Multiple signed-in accounts surface through list_accounts."""
    store, _cli = _make_store_with_users(
        tmp_path,
        [("user-1", "one@example.com", None), ("user-2", "two@example.com", None)],
    )

    accounts = store.list_accounts()
    assert len(accounts) == 2
    emails = {a.email for a in accounts}
    assert emails == {"one@example.com", "two@example.com"}


def test_invalidate_picks_up_new_account(tmp_path: Path) -> None:
    """After invalidation the store re-fetches identity from the plugin."""
    store, cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    assert {a.email for a in store.list_accounts()} == {"a@b.com"}

    cli.add_account(user_id="user-2", email="b@b.com")
    # Without invalidation the cache still holds the old list.
    assert {a.email for a in store.list_accounts()} == {"a@b.com"}

    store.invalidate_identity_cache()
    assert {a.email for a in store.list_accounts()} == {"a@b.com", "b@b.com"}


def test_remove_account_disappears_after_invalidate(tmp_path: Path) -> None:
    """Removing an account from the plugin and invalidating drops it from list_accounts."""
    store, cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    cli.remove_account("user-1")
    store.invalidate_identity_cache()

    assert store.get_session("user-1") is None
    assert store.list_accounts() == []


def test_associate_and_disassociate_workspace(tmp_path: Path) -> None:
    """Workspace association and disassociation persist on disk."""
    store, _cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])

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
    store, _cli = _make_store_with_users(
        tmp_path,
        [("user-1", "one@example.com", None), ("user-2", "two@example.com", None)],
    )
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
    store, _cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    store.associate_workspace("user-1", "agent-aaa")
    store.associate_workspace("user-1", "agent-aaa")

    session = store.get_session("user-1")
    assert session is not None
    assert session.workspace_ids == ["agent-aaa"]


def test_get_user_info(tmp_path: Path) -> None:
    """get_user_info returns a UserInfo with derived prefix."""
    store, _cli = _make_store_with_users(
        tmp_path,
        [("abcd1234-5678-9abc-def0-1234567890ab", "test@example.com", "Test User")],
    )

    info = store.get_user_info("abcd1234-5678-9abc-def0-1234567890ab")
    assert info is not None
    assert info.email == "test@example.com"
    assert info.display_name == "Test User"
    assert str(info.user_id_prefix) == "abcd123456789abc"


def test_corrupt_associations_file_returns_empty(tmp_path: Path) -> None:
    """A corrupt workspace_associations.json doesn't break list_accounts."""
    store, _cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    (tmp_path / "workspace_associations.json").write_text("not valid json {{{")
    accounts = store.list_accounts()
    assert len(accounts) == 1
    assert accounts[0].workspace_ids == []


def test_is_any_signed_in(tmp_path: Path) -> None:
    """is_any_signed_in reflects whether the plugin reports any accounts."""
    store, cli = _make_store_with_users(tmp_path, [])
    assert not store.is_any_signed_in()

    cli.add_account(user_id="user-1", email="a@b.com")
    store.invalidate_identity_cache()
    assert store.is_any_signed_in()


def test_derive_user_id_prefix() -> None:
    """derive_user_id_prefix strips hyphens and takes first 16 chars."""
    prefix = derive_user_id_prefix("abcd1234-5678-9abc-def0-1234567890ab")
    assert str(prefix) == "abcd123456789abc"


def test_disassociate_from_nonexistent_user_is_noop(tmp_path: Path) -> None:
    """Disassociating from a user with no associations does nothing."""
    store, _cli = _make_store_with_users(tmp_path, [])
    store.disassociate_workspace("nonexistent-user", "agent-xyz")


def test_disassociate_nonexistent_workspace_is_noop(tmp_path: Path) -> None:
    """Disassociating a workspace that isn't associated does nothing."""
    store, _cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    store.disassociate_workspace("user-1", "agent-not-associated")
    session = store.get_session("user-1")
    assert session is not None
    assert session.workspace_ids == []


def test_associate_for_unsigned_user_writes_disk_but_not_listed(tmp_path: Path) -> None:
    """Disk associations are independent of signed-in identity.

    Associating an agent_id with a user_id that isn't signed in persists
    the association, but list_accounts (driven by the plugin's auth
    list) won't include the user. This mirrors the post-signout state
    where workspace_ids linger until the user signs back in.
    """
    store, _cli = _make_store_with_users(tmp_path, [])
    store.associate_workspace("nonexistent-user", "agent-xyz")
    assert store.list_accounts() == []


def test_get_account_email(tmp_path: Path) -> None:
    """get_account_email returns the email for a known user_id."""
    store, _cli = _make_store_with_users(tmp_path, [("user-1", "alice@example.com", None)])
    assert store.get_account_email("user-1") == "alice@example.com"


def test_get_account_email_nonexistent_returns_none(tmp_path: Path) -> None:
    """get_account_email returns None for an unknown user_id."""
    store, _cli = _make_store_with_users(tmp_path, [])
    assert store.get_account_email("nonexistent") is None


def test_get_user_info_nonexistent_returns_none(tmp_path: Path) -> None:
    """get_user_info returns None for nonexistent user."""
    store, _cli = _make_store_with_users(tmp_path, [])
    assert store.get_user_info("nonexistent") is None


def test_has_signed_in_before_with_associations(tmp_path: Path) -> None:
    """has_signed_in_before returns True once the associations file exists."""
    store, _cli = _make_store_with_users(tmp_path, [])
    assert not store.has_signed_in_before()
    store.associate_workspace("user-1", "agent-x")
    assert store.has_signed_in_before()


def test_has_signed_in_before_when_plugin_reports_account(tmp_path: Path) -> None:
    """has_signed_in_before is True even with no associations file when the plugin has a session."""
    store, _cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    assert store.has_signed_in_before()


def test_persistence_across_store_instances(tmp_path: Path) -> None:
    """Workspace associations written by one store instance are readable by another."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="persist@test.com")
    store1 = make_session_store_for_test(tmp_path, cli=cli)
    store1.associate_workspace("user-1", "agent-xyz")

    store2 = make_session_store_for_test(tmp_path, cli=cli)
    session = store2.get_session("user-1")
    assert session is not None
    assert session.email == "persist@test.com"
    assert session.workspace_ids == ["agent-xyz"]


def test_legacy_sessions_file_migration_reads_workspace_ids(tmp_path: Path) -> None:
    """The legacy ~/.minds/sessions.json shape is read for workspace_ids on first run."""
    legacy = tmp_path / "sessions.json"
    legacy.write_text(
        '{"user-legacy": {"user_id": "user-legacy", "email": "old@example.com",'
        ' "display_name": null, "workspace_ids": ["agent-old"]}}'
    )
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-legacy", email="old@example.com")
    store = make_session_store_for_test(tmp_path, cli=cli)

    session = store.get_session("user-legacy")
    assert session is not None
    assert session.email == "old@example.com"
    assert session.workspace_ids == ["agent-old"]
