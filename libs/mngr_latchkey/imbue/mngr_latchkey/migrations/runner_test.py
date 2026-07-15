import json
from pathlib import Path

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.migrations.interface import DataFormatMigration
from imbue.mngr_latchkey.migrations.interface import LatchkeyMigrationError
from imbue.mngr_latchkey.migrations.runner import CURRENT_DATA_FORMAT_VERSION
from imbue.mngr_latchkey.migrations.runner import _sequence_migrations
from imbue.mngr_latchkey.migrations.runner import read_data_format_version
from imbue.mngr_latchkey.migrations.runner import run_data_format_migrations
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions

_LEGACY_CONFIG = LatchkeyPermissionsConfig(
    rules=(
        {"minds-workspaces": ["minds-workspaces-read"]},
        {"latchkey-self": ["latchkey-self-read-self-permissions"]},
    ),
    schemas={
        "minds-workspaces": {"properties": {"domain": {"const": "latchkey-self.invalid"}}, "required": ["domain"]},
        "minds-workspaces-read": {"properties": {"method": {"const": "GET"}}, "required": ["method"]},
    },
)


def test_read_version_defaults_to_zero_when_file_absent(tmp_path: Path) -> None:
    assert read_data_format_version(tmp_path) == 0


def test_read_version_raises_on_non_integer_contents(tmp_path: Path) -> None:
    (tmp_path / "data-format-version").write_text("not-a-number")
    with pytest.raises(LatchkeyMigrationError):
        read_data_format_version(tmp_path)


def test_run_on_fresh_store_stamps_current_version_without_touching_data(tmp_path: Path) -> None:
    run_data_format_migrations(tmp_path)
    assert read_data_format_version(tmp_path) == CURRENT_DATA_FORMAT_VERSION


def test_run_migrates_legacy_store_up_and_records_current_version(tmp_path: Path) -> None:
    host_id = HostId.generate()
    save_permissions(permissions_path_for_host(tmp_path, host_id), _LEGACY_CONFIG)

    run_data_format_migrations(tmp_path)

    assert read_data_format_version(tmp_path) == CURRENT_DATA_FORMAT_VERSION
    migrated = json.loads(permissions_path_for_host(tmp_path, host_id).read_text())
    rule_keys = [next(iter(rule.keys())) for rule in migrated["rules"]]
    assert "minds-workspaces" not in rule_keys
    assert "minds-workspaces" not in migrated["schemas"]


def test_run_is_noop_once_already_at_current_version(tmp_path: Path) -> None:
    host_id = HostId.generate()
    save_permissions(permissions_path_for_host(tmp_path, host_id), _LEGACY_CONFIG)
    run_data_format_migrations(tmp_path)
    migrated_once = permissions_path_for_host(tmp_path, host_id).read_text()

    # A second run finds the recorded version already current and rewrites nothing.
    run_data_format_migrations(tmp_path)
    assert permissions_path_for_host(tmp_path, host_id).read_text() == migrated_once


class _RecordingMigration(DataFormatMigration):
    """A no-op migration that records which direction it was invoked in, for sequencing tests."""

    applied_up_versions: list[int] = []
    applied_down_versions: list[int] = []

    def apply_up(self, plugin_data_dir: Path) -> None:
        del plugin_data_dir
        self.applied_up_versions.append(self.version)

    def apply_down(self, plugin_data_dir: Path) -> None:
        del plugin_data_dir
        self.applied_down_versions.append(self.version)


def test_sequence_applies_up_migrations_above_recorded_version_in_order(tmp_path: Path) -> None:
    first = _RecordingMigration(version=1)
    second = _RecordingMigration(version=2)

    _sequence_migrations(tmp_path, (first, second), target_version=2)

    assert first.applied_up_versions == [1]
    assert second.applied_up_versions == [2]
    assert first.applied_down_versions == []
    assert second.applied_down_versions == []
    assert read_data_format_version(tmp_path) == 2


def test_sequence_reverts_down_migrations_above_target_highest_first(tmp_path: Path) -> None:
    first = _RecordingMigration(version=1)
    second = _RecordingMigration(version=2)
    (tmp_path / "data-format-version").write_text("2\n")

    # Downgrade from recorded version 2 to target 1: only migration 2 is reverted.
    _sequence_migrations(tmp_path, (first, second), target_version=1)

    assert second.applied_down_versions == [2]
    assert first.applied_down_versions == []
    assert first.applied_up_versions == []
    assert read_data_format_version(tmp_path) == 1


def test_sequence_rejects_non_consecutive_registry(tmp_path: Path) -> None:
    with pytest.raises(LatchkeyMigrationError):
        _sequence_migrations(tmp_path, (_RecordingMigration(version=2),), target_version=2)


def test_migration_interface_rejects_non_positive_version() -> None:
    with pytest.raises(ValueError):
        _RecordingMigration(version=0)
