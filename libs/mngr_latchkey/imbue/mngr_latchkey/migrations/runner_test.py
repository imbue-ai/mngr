from collections.abc import Sequence
from pathlib import Path

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.migrations.interface import PermissionsFormatNewerError
from imbue.mngr_latchkey.migrations.interface import PermissionsMigration
from imbue.mngr_latchkey.migrations.interface import PermissionsMigrationError
from imbue.mngr_latchkey.migrations.mock_permissions_migration_test import FailingMigration
from imbue.mngr_latchkey.migrations.mock_permissions_migration_test import RuleAppendingMigration
from imbue.mngr_latchkey.migrations.runner import latest_permissions_format_version
from imbue.mngr_latchkey.migrations.runner import migrate_permissions
from imbue.mngr_latchkey.primitives import PermissionsFormatVersion
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_format_version_path
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import read_permissions_format_version
from imbue.mngr_latchkey.store import write_permissions_format_version
from imbue.mngr_latchkey.testing import MIGRATION_CONTEXT
from imbue.mngr_latchkey.testing import rule_keys_of_permissions_json
from imbue.mngr_latchkey.testing import write_raw_host_permissions

_EMPTY_POLICY = '{"rules": []}'


def _migrate(data_dir: Path, host_id: HostId, migrations: Sequence[PermissionsMigration]) -> bool:
    return migrate_permissions(data_dir, host_id, MIGRATION_CONTEXT, migrations)


def _rule_keys(data_dir: Path, host_id: HostId) -> list[str]:
    return rule_keys_of_permissions_json(permissions_path_for_host(data_dir, host_id).read_text())


def _appending(version: int, rule_key: str) -> RuleAppendingMigration:
    return RuleAppendingMigration(version=PermissionsFormatVersion(version), rule_key=rule_key)


def test_the_latest_version_is_the_highest_migration_or_zero_without_any() -> None:
    assert latest_permissions_format_version(()) == 0
    assert latest_permissions_format_version((_appending(1, "a"), _appending(2, "b"))) == 2


def test_an_unstamped_policy_gets_every_migration_in_order_and_ends_at_the_last_version(tmp_path: Path) -> None:
    """A policy from before stamps existed is at version 0, so everything runs on it, each step fed the last's result."""
    host_id = HostId.generate()
    write_raw_host_permissions(tmp_path, host_id, _EMPTY_POLICY)
    first = _appending(1, "first-6231")
    second = _appending(2, "second-6231")

    assert _migrate(tmp_path, host_id, (first, second)) is True

    assert _rule_keys(tmp_path, host_id) == ["first-6231", "second-6231"]
    assert read_permissions_format_version(tmp_path, host_id) == 2
    assert first.applied_to == [LatchkeyPermissionsConfig()]
    assert second.applied_to == [LatchkeyPermissionsConfig(rules=({"first-6231": ["any"]},))]


def test_a_policy_at_an_earlier_version_gets_only_the_migrations_above_it(tmp_path: Path) -> None:
    host_id = HostId.generate()
    write_raw_host_permissions(tmp_path, host_id, _EMPTY_POLICY)
    write_permissions_format_version(tmp_path, host_id, PermissionsFormatVersion(1))
    first = _appending(1, "first-6231")
    second = _appending(2, "second-6231")

    assert _migrate(tmp_path, host_id, (first, second)) is True

    assert _rule_keys(tmp_path, host_id) == ["second-6231"]
    assert first.applied_to == []
    assert read_permissions_format_version(tmp_path, host_id) == 2


def test_a_policy_at_the_current_version_is_left_alone(tmp_path: Path) -> None:
    host_id = HostId.generate()
    policy_path = write_raw_host_permissions(tmp_path, host_id, _EMPTY_POLICY)
    write_permissions_format_version(tmp_path, host_id, PermissionsFormatVersion(1))
    modified_at_before = policy_path.stat().st_mtime_ns
    migration = _appending(1, "first-6231")

    assert _migrate(tmp_path, host_id, (migration,)) is False

    assert migration.applied_to == []
    assert policy_path.stat().st_mtime_ns == modified_at_before


def test_a_build_without_migrations_touches_nothing(tmp_path: Path) -> None:
    host_id = HostId.generate()
    write_raw_host_permissions(tmp_path, host_id, _EMPTY_POLICY)

    assert _migrate(tmp_path, host_id, ()) is False

    assert not permissions_format_version_path(tmp_path, host_id).exists()


def test_a_host_with_no_policy_has_nothing_to_migrate_and_is_not_stamped(tmp_path: Path) -> None:
    """Its stamp arrives with a policy adopted from its machine, or is written when one is first created."""
    host_id = HostId.generate()
    migration = _appending(1, "first-6231")

    assert _migrate(tmp_path, host_id, (migration,)) is False

    assert migration.applied_to == []
    assert not permissions_format_version_path(tmp_path, host_id).exists()


def test_a_policy_stamped_newer_than_the_build_knows_is_refused_untouched(tmp_path: Path) -> None:
    """A build that does not know a format cannot edit a policy in it; only a newer build can read it."""
    host_id = HostId.generate()
    write_raw_host_permissions(tmp_path, host_id, _EMPTY_POLICY)
    write_permissions_format_version(tmp_path, host_id, PermissionsFormatVersion(3))
    migration = _appending(1, "first-6231")

    with pytest.raises(PermissionsFormatNewerError, match="format version 3, which this build does not know"):
        _migrate(tmp_path, host_id, (migration,))

    assert migration.applied_to == []
    assert _rule_keys(tmp_path, host_id) == []
    assert read_permissions_format_version(tmp_path, host_id) == 3


def test_a_failing_migration_leaves_the_stamp_at_the_last_step_that_completed(tmp_path: Path) -> None:
    """The next run picks up where this one stopped, rather than repeating what already landed."""
    host_id = HostId.generate()
    write_raw_host_permissions(tmp_path, host_id, _EMPTY_POLICY)
    first = _appending(1, "first-6231")

    with pytest.raises(PermissionsMigrationError, match="migration 2 cannot rewrite"):
        _migrate(tmp_path, host_id, (first, FailingMigration(version=PermissionsFormatVersion(2))))

    assert _rule_keys(tmp_path, host_id) == ["first-6231"]
    assert read_permissions_format_version(tmp_path, host_id) == 1

    second = _appending(2, "second-6231")
    assert _migrate(tmp_path, host_id, (first, second)) is True
    assert len(first.applied_to) == 1
    assert _rule_keys(tmp_path, host_id) == ["first-6231", "second-6231"]
    assert read_permissions_format_version(tmp_path, host_id) == 2


@pytest.mark.parametrize("versions", [(2,), (1, 3), (1, 1)])
def test_migrations_must_be_numbered_consecutively_from_one(tmp_path: Path, versions: tuple[int, ...]) -> None:
    host_id = HostId.generate()
    write_raw_host_permissions(tmp_path, host_id, _EMPTY_POLICY)
    migrations = tuple(_appending(version, f"rule-{version}") for version in versions)

    with pytest.raises(PermissionsMigrationError, match="not numbered consecutively from 1"):
        _migrate(tmp_path, host_id, migrations)

    assert _rule_keys(tmp_path, host_id) == []
    assert all(migration.applied_to == [] for migration in migrations)


def test_an_unreadable_stamp_is_reported_rather_than_read_as_zero(tmp_path: Path) -> None:
    """Running every migration over a policy whose version is unknown could corrupt it; refusing cannot."""
    host_id = HostId.generate()
    write_raw_host_permissions(tmp_path, host_id, _EMPTY_POLICY)
    permissions_format_version_path(tmp_path, host_id).write_text("banana\n")
    migration = _appending(1, "first-6231")

    with pytest.raises(PermissionsMigrationError, match="not a non-negative integer"):
        _migrate(tmp_path, host_id, (migration,))

    assert migration.applied_to == []


def test_an_unreadable_policy_is_reported_rather_than_migrated(tmp_path: Path) -> None:
    host_id = HostId.generate()
    policy_path = write_raw_host_permissions(tmp_path, host_id, "not json")
    migration = _appending(1, "first-6231")

    with pytest.raises(PermissionsMigrationError, match="malformed"):
        _migrate(tmp_path, host_id, (migration,))

    assert migration.applied_to == []
    assert policy_path.read_text() == "not json"
    assert not permissions_format_version_path(tmp_path, host_id).exists()
