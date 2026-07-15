import json
from pathlib import Path

import pytest

from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.session_store import derive_user_id_prefix
from imbue.minds.errors import WorkspaceSyncError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

_AGENT_A = str(AgentId.generate())
_AGENT_B = str(AgentId.generate())


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


def _resolver_for_agents(*agent_ids: str) -> MngrCliBackendResolver:
    """Build a resolver where each agent lives on its own host (distinct host_ids)."""
    agents = [
        {
            "id": agent_id,
            "labels": {"is_primary": "true"},
            "host": {"id": str(HostId.generate()), "name": agent_id[:12]},
        }
        for agent_id in agent_ids
    ]
    return make_resolver_with_data(agents_json=json.dumps({"agents": agents}))


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
    """Association creates a workspace record; disassociation removes it."""
    store, cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    resolver = _resolver_for_agents(_AGENT_A, _AGENT_B)

    store.associate_workspace("user-1", _AGENT_A, resolver)
    store.associate_workspace("user-1", _AGENT_B, resolver)

    session = store.get_session("user-1")
    assert session is not None
    assert sorted(session.workspace_ids) == sorted([_AGENT_A, _AGENT_B])
    # The records landed on the (fake) connector.
    assert len(cli.sync_records_by_email["a@b.com"]) == 2

    store.disassociate_workspace("user-1", _AGENT_A)
    session = store.get_session("user-1")
    assert session is not None
    assert session.workspace_ids == [_AGENT_B]
    assert len(cli.sync_records_by_email["a@b.com"]) == 1


def test_get_account_for_workspace(tmp_path: Path) -> None:
    """Can look up which account a workspace belongs to."""
    store, _cli = _make_store_with_users(
        tmp_path,
        [("user-1", "one@example.com", None), ("user-2", "two@example.com", None)],
    )
    resolver = _resolver_for_agents(_AGENT_A, _AGENT_B)
    store.associate_workspace("user-1", _AGENT_A, resolver)
    store.associate_workspace("user-2", _AGENT_B, resolver)

    account = store.get_account_for_workspace(_AGENT_A)
    assert account is not None
    assert account.email == "one@example.com"

    account = store.get_account_for_workspace(_AGENT_B)
    assert account is not None
    assert account.email == "two@example.com"

    assert store.get_account_for_workspace("agent-unknown") is None


def test_duplicate_associate_is_idempotent(tmp_path: Path) -> None:
    """Associating the same workspace twice doesn't create duplicates."""
    store, _cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    resolver = _resolver_for_agents(_AGENT_A)
    store.associate_workspace("user-1", _AGENT_A, resolver)
    store.associate_workspace("user-1", _AGENT_A, resolver)

    session = store.get_session("user-1")
    assert session is not None
    assert session.workspace_ids == [_AGENT_A]


def test_associate_while_offline_raises(tmp_path: Path) -> None:
    """Settings-page association requires connectivity and fails cleanly offline."""
    store, cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    cli.is_sync_offline = True
    resolver = _resolver_for_agents(_AGENT_A)

    with pytest.raises(WorkspaceSyncError):
        store.associate_workspace("user-1", _AGENT_A, resolver)
    assert store.get_account_for_workspace(_AGENT_A) is None


def test_associate_created_workspace_seeds_a_queued_record(tmp_path: Path) -> None:
    """The create-path association seeds a record with form metadata (no resolver needed)."""
    store, cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])

    store.associate_created_workspace(
        user_id="user-1",
        agent_id="agent-new",
        host_id="host-new",
        display_name="my new workspace",
        color="#112233",
        is_cloud_row=False,
    )

    session = store.get_session("user-1")
    assert session is not None
    assert session.workspace_ids == ["agent-new"]
    pushed = cli.sync_records_by_email["a@b.com"]["host-new"]
    assert pushed["display_name"] == "my new workspace"
    assert pushed["color"] == "#112233"
    assert pushed["hosting_device_id"] == "device-test"


def test_associate_created_workspace_queues_offline(tmp_path: Path) -> None:
    """A connector outage never fails creation: the record queues locally."""
    store, cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    cli.is_sync_offline = True

    store.associate_created_workspace(
        user_id="user-1",
        agent_id="agent-new",
        host_id="host-new",
        display_name="ws",
        color=None,
        is_cloud_row=False,
    )

    session = store.get_session("user-1")
    assert session is not None
    assert session.workspace_ids == ["agent-new"]
    assert "a@b.com" not in cli.sync_records_by_email


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


def test_disassociate_from_unknown_user_raises(tmp_path: Path) -> None:
    """Disassociating for a user that isn't signed in raises (no account to resolve)."""
    store, _cli = _make_store_with_users(tmp_path, [])
    with pytest.raises(WorkspaceSyncError):
        store.disassociate_workspace("nonexistent-user", "agent-xyz")


def test_disassociate_nonexistent_workspace_is_noop(tmp_path: Path) -> None:
    """Disassociating a workspace that isn't associated does nothing."""
    store, _cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    store.disassociate_workspace("user-1", "agent-not-associated")
    session = store.get_session("user-1")
    assert session is not None
    assert session.workspace_ids == []


def test_associate_for_unsigned_user_raises(tmp_path: Path) -> None:
    """Associating with a user_id that isn't signed in raises instead of writing state."""
    store, _cli = _make_store_with_users(tmp_path, [])
    resolver = _resolver_for_agents(_AGENT_A)
    with pytest.raises(WorkspaceSyncError):
        store.associate_workspace("nonexistent-user", _AGENT_A, resolver)
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
    """has_signed_in_before returns True once any workspace record exists."""
    store, cli = _make_store_with_users(tmp_path, [])
    assert not store.has_signed_in_before()
    cli.add_account(user_id="user-1", email="a@b.com")
    store.invalidate_identity_cache()
    store.associate_created_workspace(
        user_id="user-1", agent_id="agent-x", host_id="host-x", display_name="x", color=None, is_cloud_row=False
    )
    assert store.has_signed_in_before()


def test_has_signed_in_before_when_plugin_reports_account(tmp_path: Path) -> None:
    """has_signed_in_before is True even with no records when the plugin has a session."""
    store, _cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    assert store.has_signed_in_before()


def test_has_signed_in_before_with_legacy_files(tmp_path: Path) -> None:
    """A pre-sync install's legacy files still count as having signed in."""
    store, _cli = _make_store_with_users(tmp_path, [])
    (tmp_path / "workspace_associations.json").write_text("{}")
    assert store.has_signed_in_before()


def test_has_signed_in_before_with_retired_legacy_files(tmp_path: Path) -> None:
    """Legacy files renamed aside by the one-shot conversion still count as having signed in."""
    store, _cli = _make_store_with_users(tmp_path, [])
    assert not store.has_signed_in_before()
    (tmp_path / "sessions.json.pre-sync").write_text("{}")
    assert store.has_signed_in_before()


def test_persistence_across_store_instances(tmp_path: Path) -> None:
    """Workspace records written by one store instance are readable by another."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="persist@test.com")
    store1 = make_session_store_for_test(tmp_path, cli=cli)
    resolver = _resolver_for_agents(_AGENT_A)
    store1.associate_workspace("user-1", _AGENT_A, resolver)

    store2 = make_session_store_for_test(tmp_path, cli=cli)
    session = store2.get_session("user-1")
    assert session is not None
    assert session.email == "persist@test.com"
    assert session.workspace_ids == [_AGENT_A]
