import json
import os
from pathlib import Path

import pytest

from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.session_store import derive_user_id_prefix
from imbue.minds.desktop_client.testing import device_id_for_test
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


def _make_plugin_host_dir(tmp_path: Path) -> tuple[Path, Path]:
    """Fabricate the mngr host dir layout the plugin keeps its sessions under.

    Returns ``(host_dir, sessions_dir)`` where ``sessions_dir`` is
    ``<host_dir>/profiles/<profile>/providers/imbue_cloud/sessions`` -- the
    directory ``mngr imbue_cloud auth signin``/``signout`` write to.
    """
    host_dir = tmp_path / "mngr-host"
    sessions_dir = host_dir / "profiles" / "profile0" / "providers" / "imbue_cloud" / "sessions"
    sessions_dir.mkdir(parents=True)
    (host_dir / "config.toml").write_text('profile = "profile0"\n')
    return host_dir, sessions_dir


def test_out_of_band_plugin_signin_surfaces_without_invalidate(tmp_path: Path) -> None:
    """A CLI signin's on-disk session write refreshes the identity cache on the next read."""
    host_dir, sessions_dir = _make_plugin_host_dir(tmp_path)
    cli = make_fake_imbue_cloud_cli()
    store = make_session_store_for_test(tmp_path / "data", cli=cli, mngr_host_dir=host_dir)
    assert store.list_accounts() == []

    # The plugin gains an account but nothing on disk changed yet (only the
    # fake's in-memory listing): the cached empty listing keeps being served.
    cli.add_account(user_id="user-1", email="cli@example.com")
    assert store.list_accounts() == []

    # The signin's session-file write is what invalidates the cache.
    (sessions_dir / "user-1.json").write_text("{}")
    assert {a.email for a in store.list_accounts()} == {"cli@example.com"}


def test_out_of_band_plugin_signout_surfaces_without_invalidate(tmp_path: Path) -> None:
    """Deleting a session file (CLI signout) drops the account on the next read."""
    host_dir, sessions_dir = _make_plugin_host_dir(tmp_path)
    session_file = sessions_dir / "user-1.json"
    session_file.write_text("{}")
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="cli@example.com")
    store = make_session_store_for_test(tmp_path / "data", cli=cli, mngr_host_dir=host_dir)
    assert {a.email for a in store.list_accounts()} == {"cli@example.com"}

    cli.remove_account("user-1")
    # Disk unchanged: the cache still lists the account.
    assert {a.email for a in store.list_accounts()} == {"cli@example.com"}

    session_file.unlink()
    assert store.list_accounts() == []


def test_atomic_same_size_same_mtime_rewrite_surfaces(tmp_path: Path) -> None:
    """An atomic same-size rewrite landing in the same mtime tick still refreshes the cache.

    The plugin writes session files via temp-file-plus-os.replace, so every
    rewrite mints a new inode even when the payload size is unchanged and the
    coarse clock hands it the same mtime (forced here via os.utime); the
    fingerprint's inode component is what catches this case.
    """
    host_dir, sessions_dir = _make_plugin_host_dir(tmp_path)
    session_file = sessions_dir / "user-1.json"
    session_file.write_text('{"n": 1}')
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id="user-1", email="old@example.com")
    store = make_session_store_for_test(tmp_path / "data", cli=cli, mngr_host_dir=host_dir)
    assert {a.email for a in store.list_accounts()} == {"old@example.com"}

    original_stats = session_file.stat()
    cli.remove_account("user-1")
    cli.add_account(user_id="user-1", email="new@example.com")
    # Simulate the plugin's atomic rewrite: same name, same size, and (forced)
    # the exact same mtime -- only the inode differs.
    replacement = sessions_dir / "user-1.json.tmp"
    replacement.write_text('{"n": 2}')
    os.replace(replacement, session_file)
    os.utime(session_file, ns=(original_stats.st_atime_ns, original_stats.st_mtime_ns))
    assert session_file.stat().st_size == original_stats.st_size
    assert {a.email for a in store.list_accounts()} == {"new@example.com"}


def test_sessions_dir_appearing_later_is_picked_up(tmp_path: Path) -> None:
    """A host dir initialized after the store was built still gets the coherence check."""
    host_dir = tmp_path / "mngr-host"
    host_dir.mkdir()
    cli = make_fake_imbue_cloud_cli()
    # No config.toml yet: the sessions dir cannot be resolved, so the first
    # read caches against "unresolvable".
    store = make_session_store_for_test(tmp_path / "data", cli=cli, mngr_host_dir=host_dir)
    assert store.list_accounts() == []

    sessions_dir = host_dir / "profiles" / "profile0" / "providers" / "imbue_cloud" / "sessions"
    sessions_dir.mkdir(parents=True)
    (host_dir / "config.toml").write_text('profile = "profile0"\n')
    (sessions_dir / "user-1.json").write_text("{}")
    cli.add_account(user_id="user-1", email="late@example.com")
    assert {a.email for a in store.list_accounts()} == {"late@example.com"}


def test_remove_account_disappears_after_invalidate(tmp_path: Path) -> None:
    """Removing an account from the plugin and invalidating drops it from list_accounts."""
    store, cli = _make_store_with_users(tmp_path, [("user-1", "a@b.com", None)])
    cli.remove_account("user-1")
    store.invalidate_identity_cache()

    assert store.get_session("user-1") is None
    assert store.list_accounts() == []


def test_associate_and_disassociate_workspace(tmp_path: Path) -> None:
    """Association creates a machine record; disassociation removes it."""
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
    """Can look up which account a machine belongs to."""
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
    """Associating the same machine twice doesn't create duplicates."""
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
        display_name="my new machine",
        color="#112233",
        is_cloud_row=False,
    )

    session = store.get_session("user-1")
    assert session is not None
    assert session.workspace_ids == ["agent-new"]
    pushed = cli.sync_records_by_email["a@b.com"]["host-new"]
    assert pushed["display_name"] == "my new machine"
    assert pushed["color"] == "#112233"
    assert pushed["hosting_device_id"] == device_id_for_test("session-store")


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
    """Disassociating a machine that isn't associated does nothing."""
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


def test_persistence_across_store_instances(tmp_path: Path) -> None:
    """Machine records written by one store instance are readable by another."""
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
