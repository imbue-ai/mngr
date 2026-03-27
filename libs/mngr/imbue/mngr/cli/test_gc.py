"""Integration tests for the gc CLI command."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pluggy
from click.testing import CliRunner

from imbue.mngr.cli.gc import gc
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.providers.local.instance import get_or_create_local_host_id


def _write_certified_data(per_host_dir: Path, temp_host_dir: Path, generated_work_dirs: tuple[str, ...]) -> Path:
    """Write CertifiedHostData to data.json in the per-host directory. Returns data_path."""
    host_id = get_or_create_local_host_id(temp_host_dir)
    now = datetime.now(timezone.utc)
    certified_data = CertifiedHostData(
        host_id=str(host_id),
        host_name="test-host",
        generated_work_dirs=generated_work_dirs,
        created_at=now,
        updated_at=now,
    )
    data_path = per_host_dir / "data.json"
    data_path.write_text(json.dumps(certified_data.model_dump(by_alias=True, mode="json"), indent=2))
    return data_path


def test_gc_work_dirs_dry_run(
    cli_runner: CliRunner,
    temp_host_dir: Path,
    per_host_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test gc --dry-run shows orphaned directories without removing them."""
    orphaned_dir = temp_host_dir / "worktrees" / "orphaned-agent-123"
    orphaned_dir.mkdir(parents=True)

    _write_certified_data(per_host_dir, temp_host_dir, (str(orphaned_dir),))

    result = cli_runner.invoke(
        gc,
        ["--dry-run"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Would destroy" in result.output
    assert str(orphaned_dir) in result.output
    assert orphaned_dir.exists(), "Directory should still exist after dry-run"


def test_gc_removes_orphaned_directory(
    cli_runner: CliRunner,
    temp_host_dir: Path,
    per_host_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test gc removes orphaned directories and updates certified data."""
    orphaned_dir = temp_host_dir / "worktrees" / "orphaned-agent-456"
    orphaned_dir.mkdir(parents=True)

    test_file = orphaned_dir / "test.txt"
    test_file.write_text("test content")

    data_path = _write_certified_data(per_host_dir, temp_host_dir, (str(orphaned_dir),))

    result = cli_runner.invoke(
        gc,
        [],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Work directories: 1" in result.output
    assert "Destroyed 1 resource(s)" in result.output
    assert not orphaned_dir.exists(), "Orphaned directory should be removed"

    updated_data = CertifiedHostData.model_validate_json(data_path.read_text())
    assert str(orphaned_dir) not in updated_data.generated_work_dirs, "generated_work_dirs should be updated"
