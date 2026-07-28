import json
from pathlib import Path

from imbue.mngr_latchkey.migrations.add_shared_schemas_include import AddSharedSchemasInclude

_SHARED_SCHEMAS_FILENAME = "minds_shared_schemas.json"


def _host_permissions_path(plugin_data_dir: Path, host_id: str) -> Path:
    return plugin_data_dir / "hosts" / host_id / "latchkey_permissions.json"


def _write_host_file(plugin_data_dir: Path, host_id: str, content: dict[str, object]) -> Path:
    path = _host_permissions_path(plugin_data_dir, host_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content))
    return path


def test_apply_up_adds_shared_schemas_include(tmp_path: Path) -> None:
    """A per-host file that predates the include gets the shared-schemas include stamped in."""
    path = _write_host_file(tmp_path, "host-a", {"rules": [{"slack-api": ["slack-read-all"]}], "schemas": {"x": {}}})

    AddSharedSchemasInclude(version=3).apply_up(tmp_path, tmp_path, "latchkey")

    migrated = json.loads(path.read_text())
    assert migrated["include"] == [_SHARED_SCHEMAS_FILENAME]
    # Rules and schemas are untouched.
    assert migrated["rules"] == [{"slack-api": ["slack-read-all"]}]
    assert migrated["schemas"] == {"x": {}}


def test_apply_up_is_idempotent(tmp_path: Path) -> None:
    """Re-applying the up migration does not duplicate the include."""
    path = _write_host_file(tmp_path, "host-a", {"rules": [], "include": [_SHARED_SCHEMAS_FILENAME]})

    AddSharedSchemasInclude(version=3).apply_up(tmp_path, tmp_path, "latchkey")

    assert json.loads(path.read_text())["include"] == [_SHARED_SCHEMAS_FILENAME]


def test_apply_down_removes_only_the_shared_schemas_include(tmp_path: Path) -> None:
    """The down migration strips the shared-schemas include but preserves any other includes."""
    path = _write_host_file(tmp_path, "host-a", {"rules": [], "include": ["other.json", _SHARED_SCHEMAS_FILENAME]})

    AddSharedSchemasInclude(version=3).apply_down(tmp_path, tmp_path, "latchkey")

    migrated = json.loads(path.read_text())
    assert migrated["include"] == ["other.json"]


def test_apply_down_omits_empty_include(tmp_path: Path) -> None:
    """Removing the only include drops the ``include`` key entirely (matching save_permissions)."""
    path = _write_host_file(tmp_path, "host-a", {"rules": [], "include": [_SHARED_SCHEMAS_FILENAME]})

    AddSharedSchemasInclude(version=3).apply_down(tmp_path, tmp_path, "latchkey")

    assert "include" not in json.loads(path.read_text())


def test_migration_is_a_noop_when_no_host_files_exist(tmp_path: Path) -> None:
    """A fresh store with no per-host files migrates cleanly (nothing to rewrite)."""
    AddSharedSchemasInclude(version=3).apply_up(tmp_path, tmp_path, "latchkey")
    assert not (tmp_path / "hosts").exists()
