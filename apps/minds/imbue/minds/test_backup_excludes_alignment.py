"""Verify the restore script's fallback snapshot excludes match host_backup's built-in defaults.

The restore script takes a pre-restore safety snapshot and a restored-state
snapshot, which should leave out what the workspace's hourly backups leave out.
It is stdlib-only and runs inside the workspace, so it cannot import
host_backup's `BackupConfig` and keeps its own copy of the default excludes for
workspaces whose `backup.toml` sets none. This reads both lists and asserts they
agree, so a default added to one side cannot silently drift from the other.
"""

import ast

import pytest

from imbue.minds.desktop_client.backup_workspace_scripts import BACKUP_RESTORE_SCRIPT
from imbue.minds.testing import DEFAULT_WORKSPACE_TEMPLATE_OWNER_REPO
from imbue.minds.testing import fetch_default_workspace_template_file

_TEMPLATE_BACKUP_CONFIG_PATH = "system/services/host_backup/src/host_backup/config.py"


def _parse_restore_script_default_excludes(script_source: str) -> tuple[str, ...]:
    for node in ast.parse(script_source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_DEFAULT_SNAPSHOT_EXCLUDES" for target in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    pytest.fail("_DEFAULT_SNAPSHOT_EXCLUDES not found in BACKUP_RESTORE_SCRIPT; update this test alongside it.")


def _parse_backup_config_default_excludes(config_source: str) -> tuple[str, ...]:
    for node in ast.parse(config_source).body:
        if not (isinstance(node, ast.ClassDef) and node.name == "BackupConfig"):
            continue
        for statement in node.body:
            if (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == "excludes"
                and isinstance(statement.value, ast.Call)
            ):
                for keyword in statement.value.keywords:
                    if keyword.arg == "default":
                        return tuple(ast.literal_eval(keyword.value))
    pytest.fail(f"BackupConfig.excludes default not found in {_TEMPLATE_BACKUP_CONFIG_PATH}; update this test.")


@pytest.mark.release
def test_restore_snapshot_default_excludes_match_host_backup_defaults() -> None:
    config_source = fetch_default_workspace_template_file(_TEMPLATE_BACKUP_CONFIG_PATH)
    assert config_source is not None, (
        f"Failed to fetch {DEFAULT_WORKSPACE_TEMPLATE_OWNER_REPO}:{_TEMPLATE_BACKUP_CONFIG_PATH}. "
        "Check template repo reachability."
    )

    restore_defaults = _parse_restore_script_default_excludes(BACKUP_RESTORE_SCRIPT)
    host_backup_defaults = _parse_backup_config_default_excludes(config_source)

    assert set(restore_defaults) == set(host_backup_defaults), (
        "The restore script's _DEFAULT_SNAPSHOT_EXCLUDES (backup_workspace_scripts.py) has drifted from "
        f"host_backup's BackupConfig.excludes default in {DEFAULT_WORKSPACE_TEMPLATE_OWNER_REPO}; "
        f"only in the restore script: {sorted(set(restore_defaults) - set(host_backup_defaults))}, "
        f"only in host_backup: {sorted(set(host_backup_defaults) - set(restore_defaults))}."
    )
