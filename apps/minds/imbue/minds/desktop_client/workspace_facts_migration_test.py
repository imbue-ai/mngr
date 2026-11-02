import json
from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.workspace_facts_cache import WorkspaceFactsCache
from imbue.minds.desktop_client.workspace_facts_migration import migrate_per_workspace_facts
from imbue.mngr.primitives import AgentId

_DISMISSED_AT = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _setup(tmp_path: Path) -> tuple[MindsConfig, WorkspaceFactsCache, InstallationPaths]:
    paths = InstallationPaths(data_dir=tmp_path / "data")
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    return (
        MindsConfig(data_dir=tmp_path / "config"),
        WorkspaceFactsCache(records_dir=paths.data_dir / "workspace_facts_cache"),
        paths,
    )


def _write_legacy_stores(minds_config: MindsConfig, paths: InstallationPaths, agent_id: AgentId) -> None:
    (minds_config.data_dir / "config.toml").parent.mkdir(parents=True, exist_ok=True)
    (minds_config.data_dir / "config.toml").write_text(f'[updates.skipped_targets]\n"{agent_id}" = "minds-v0.8.0"\n')
    dismissals_dir = paths.data_dir / "update_dismissals"
    dismissals_dir.mkdir(parents=True)
    (dismissals_dir / f"{agent_id}.json").write_text(
        json.dumps({"agent_id": str(agent_id), "note_run_started_at": _DISMISSED_AT.isoformat()})
    )
    markers_dir = paths.data_dir / "backup_verification_disabled"
    markers_dir.mkdir(parents=True)
    (markers_dir / str(agent_id)).touch()


def test_the_migration_seeds_dirty_records_and_removes_the_legacy_stores(tmp_path: Path) -> None:
    minds_config, facts_cache, paths = _setup(tmp_path)
    agent_id = AgentId.generate()
    _write_legacy_stores(minds_config, paths, agent_id)

    assert migrate_per_workspace_facts(minds_config, facts_cache, paths) is True

    record = facts_cache.read(agent_id)
    assert (record.skipped_target_ref, record.is_skip_dirty) == ("minds-v0.8.0", True)
    assert (record.note_run_started_at, record.is_note_dirty) == (_DISMISSED_AT, True)
    assert record.outcome_run_started_at is None and record.is_outcome_dirty is False
    assert (record.is_backup_verification_disabled, record.is_backup_verification_dirty) == (True, True)
    assert minds_config.read_skipped_update_targets_for_migration() == {}
    assert not (paths.data_dir / "update_dismissals").exists()
    assert not (paths.data_dir / "backup_verification_disabled").exists()


def test_the_migration_never_overwrites_an_existing_cache_record(tmp_path: Path) -> None:
    minds_config, facts_cache, paths = _setup(tmp_path)
    agent_id = AgentId.generate()
    _write_legacy_stores(minds_config, paths, agent_id)
    facts_cache.record_skip(agent_id, "minds-v0.9.0")

    migrate_per_workspace_facts(minds_config, facts_cache, paths)

    assert facts_cache.read(agent_id).skipped_target_ref == "minds-v0.9.0"


def test_a_start_with_nothing_to_move_is_a_noop(tmp_path: Path) -> None:
    minds_config, facts_cache, paths = _setup(tmp_path)

    assert migrate_per_workspace_facts(minds_config, facts_cache, paths) is False
