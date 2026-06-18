"""Unit tests for the pure opencode_config builders, readers, and path helpers."""

import importlib.resources
import json
import sqlite3
from pathlib import Path

import pytest

from imbue.mngr.api.preservation import iter_agent_session_paths
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import HostName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_opencode import resources as _opencode_resources
from imbue.mngr_opencode.opencode_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_opencode.opencode_config import AGENT_OPENCODE_DB_RELPATH
from imbue.mngr_opencode.opencode_config import COMMON_TRANSCRIPT_RELATIVE_PATH
from imbue.mngr_opencode.opencode_config import COMMON_TRANSCRIPT_SOURCE
from imbue.mngr_opencode.opencode_config import EMIT_COMMON_ENV_VAR
from imbue.mngr_opencode.opencode_config import LAUNCH_SCRIPT_NAME
from imbue.mngr_opencode.opencode_config import OPENCODE_BIN_ENV_VAR
from imbue.mngr_opencode.opencode_config import OPENCODE_PORT_ENV_VAR
from imbue.mngr_opencode.opencode_config import OPENCODE_WORKDIR_ENV_VAR
from imbue.mngr_opencode.opencode_config import PLUGIN_FILENAME
from imbue.mngr_opencode.opencode_config import RAW_TRANSCRIPT_RELATIVE_PATH
from imbue.mngr_opencode.opencode_config import READY_SENTINEL_FILENAME
from imbue.mngr_opencode.opencode_config import ROLE_ENV_VAR
from imbue.mngr_opencode.opencode_config import ROOT_SESSION_FILENAME
from imbue.mngr_opencode.opencode_config import SERVER_PORT_FILENAME
from imbue.mngr_opencode.opencode_config import SERVER_ROLE
from imbue.mngr_opencode.opencode_config import apply_opencode_merge
from imbue.mngr_opencode.opencode_config import apply_opencode_rebind
from imbue.mngr_opencode.opencode_config import build_opencode_config
from imbue.mngr_opencode.opencode_config import build_opencode_rebind_sql
from imbue.mngr_opencode.opencode_config import collect_adopt_search_db_paths
from imbue.mngr_opencode.opencode_config import get_opencode_app_data_dir
from imbue.mngr_opencode.opencode_config import get_opencode_auth_path_for_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_config_dir
from imbue.mngr_opencode.opencode_config import get_opencode_config_file_path
from imbue.mngr_opencode.opencode_config import get_opencode_data_home
from imbue.mngr_opencode.opencode_config import get_opencode_plugin_path
from imbue.mngr_opencode.opencode_config import get_opencode_root_session_file_path
from imbue.mngr_opencode.opencode_config import get_opencode_server_port_file_path
from imbue.mngr_opencode.opencode_config import get_shared_opencode_auth_path
from imbue.mngr_opencode.opencode_config import list_source_merge_tables
from imbue.mngr_opencode.opencode_config import read_opencode_config
from imbue.mngr_opencode.opencode_config import resolve_adopt_session_db
from imbue.mngr_opencode.opencode_config import serialize_opencode_config
from imbue.mngr_opencode.testing import write_opencode_session


def test_build_opencode_config_always_sets_schema() -> None:
    config = build_opencode_config({}, {}, False)
    assert config["$schema"] == "https://opencode.ai/config.json"


def test_build_opencode_config_layers_base_then_overrides() -> None:
    config = build_opencode_config({"theme": "dark", "model": "old/m"}, {"model": "new/m"}, False)
    assert config["theme"] == "dark"
    assert config["model"] == "new/m"


def test_build_opencode_config_auto_allow_injects_wildcard_then_overrides_win() -> None:
    """auto-allow injects a wildcard permission; an explicit permission override still wins."""
    auto_only = build_opencode_config({}, {}, True)
    assert auto_only["permission"] == {"*": "allow"}
    overridden = build_opencode_config({}, {"permission": {"bash": "deny"}}, True)
    assert overridden["permission"] == {"bash": "deny"}


def test_build_opencode_config_omits_autoupdate_by_default() -> None:
    """Without disable_auto_update, no autoupdate key is set (opencode defaults to enabled)."""
    config = build_opencode_config({}, {}, False)
    assert "autoupdate" not in config


def test_build_opencode_config_disables_autoupdate_when_requested() -> None:
    config = build_opencode_config({}, {}, False, disable_auto_update=True)
    assert config["autoupdate"] is False


def test_build_opencode_config_autoupdate_override_wins() -> None:
    """An explicit autoupdate in config_overrides wins over the disable flag."""
    config = build_opencode_config({}, {"autoupdate": "notify"}, False, disable_auto_update=True)
    assert config["autoupdate"] == "notify"


def test_build_opencode_config_does_not_mutate_base() -> None:
    base = {"theme": "dark"}
    build_opencode_config(base, {"model": "m"}, True)
    assert base == {"theme": "dark"}


def test_serialize_opencode_config_round_trips() -> None:
    config = {"model": "a/b", "permission": {"*": "allow"}}
    assert json.loads(serialize_opencode_config(config)) == config


def _local_host(local_provider: LocalProviderInstance) -> OnlineHostInterface:
    return local_provider.create_host(HostName(LOCAL_HOST_NAME))


def test_read_opencode_config_missing_file_returns_empty(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    assert read_opencode_config(_local_host(local_provider), tmp_path / "absent.json") == {}


def test_read_opencode_config_empty_file_returns_empty(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    config_path = tmp_path / "opencode.json"
    config_path.write_text("   \n")
    assert read_opencode_config(_local_host(local_provider), config_path) == {}


def test_read_opencode_config_valid_object(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    config_path = tmp_path / "opencode.json"
    config_path.write_text('{"model": "a/b"}')
    assert read_opencode_config(_local_host(local_provider), config_path) == {"model": "a/b"}


def test_read_opencode_config_malformed_json_raises(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    config_path = tmp_path / "opencode.json"
    config_path.write_text("{not json")
    with pytest.raises(UserInputError):
        read_opencode_config(_local_host(local_provider), config_path)


def test_read_opencode_config_non_object_raises(local_provider: LocalProviderInstance, tmp_path: Path) -> None:
    config_path = tmp_path / "opencode.json"
    config_path.write_text("[1, 2, 3]")
    with pytest.raises(UserInputError):
        read_opencode_config(_local_host(local_provider), config_path)


def test_path_helpers_layout(tmp_path: Path) -> None:
    state = tmp_path / "agent"
    config_dir = get_opencode_config_dir(state)
    data_home = get_opencode_data_home(state)
    assert config_dir == state / "plugin" / "opencode" / "config"
    assert data_home == state / "plugin" / "opencode" / "data"
    assert get_opencode_config_file_path(config_dir) == config_dir / "opencode.json"
    assert get_opencode_plugin_path(config_dir) == config_dir / "plugin" / PLUGIN_FILENAME
    assert get_opencode_app_data_dir(data_home) == data_home / "opencode"
    assert get_opencode_auth_path_for_data_home(data_home) == data_home / "opencode" / "auth.json"
    assert get_opencode_root_session_file_path(state) == state / ROOT_SESSION_FILENAME
    assert get_opencode_server_port_file_path(state) == state / SERVER_PORT_FILENAME


def test_shared_auth_path_under_xdg_default(tmp_path: Path) -> None:
    assert get_shared_opencode_auth_path(tmp_path) == tmp_path / ".local" / "share" / "opencode" / "auth.json"


def test_plugin_resource_literals_stay_in_sync_with_constants() -> None:
    """The TS plugin hardcodes filenames/paths/role the Python side owns; guard against drift.

    The plugin can't import opencode_config.py, so it duplicates these literals.
    If a constant changes here without the .ts being updated, the marker / raw +
    common capture / role guard would silently break -- this test fails loudly.
    """
    plugin_source = importlib.resources.files(_opencode_resources).joinpath(PLUGIN_FILENAME).read_text()
    assert f'"{ACTIVE_MARKER_FILENAME}"' in plugin_source
    assert f'"{RAW_TRANSCRIPT_RELATIVE_PATH}"' in plugin_source
    # The plugin now also emits the common transcript in-process.
    assert f'"{COMMON_TRANSCRIPT_RELATIVE_PATH}"' in plugin_source
    assert f'"{COMMON_TRANSCRIPT_SOURCE}"' in plugin_source
    assert f'"{ROLE_ENV_VAR}"' in plugin_source
    assert f'"{SERVER_ROLE}"' in plugin_source
    assert f'"{EMIT_COMMON_ENV_VAR}"' in plugin_source


def test_launch_script_literals_stay_in_sync_with_constants() -> None:
    """The launch script hardcodes the file names / role / env vars the Python side owns."""
    launch_source = importlib.resources.files(_opencode_resources).joinpath(LAUNCH_SCRIPT_NAME).read_text()
    assert ROOT_SESSION_FILENAME in launch_source
    assert SERVER_PORT_FILENAME in launch_source
    assert READY_SENTINEL_FILENAME in launch_source
    assert f"{ROLE_ENV_VAR}={SERVER_ROLE}" in launch_source
    for env_var in (OPENCODE_BIN_ENV_VAR, OPENCODE_PORT_ENV_VAR, OPENCODE_WORKDIR_ENV_VAR):
        assert env_var in launch_source


def test_resolve_adopt_session_db_by_path_uses_lone_root_session(tmp_path: Path) -> None:
    db_path = tmp_path / "opencode.db"
    write_opencode_session(db_path, "ses_root", "/src/work")
    session_id, source_db = resolve_adopt_session_db(str(db_path), [])
    assert session_id == "ses_root"
    assert source_db == db_path


def test_resolve_adopt_session_db_by_path_rejects_multiple_roots(tmp_path: Path) -> None:
    db_path = tmp_path / "opencode.db"
    write_opencode_session(db_path, "ses_root", "/src/work")
    # A second parent-less session makes the db ambiguous when addressed by path.
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO session (id, project_id, parent_id, directory) VALUES (?, ?, NULL, ?)",
        ("ses_other", "proj_ses_root", "/src/work"),
    )
    connection.commit()
    connection.close()
    with pytest.raises(UserInputError, match="root sessions"):
        resolve_adopt_session_db(str(db_path), [])


def test_resolve_adopt_session_db_by_id_searches_stores(tmp_path: Path) -> None:
    db_a = tmp_path / "a" / "opencode.db"
    db_b = tmp_path / "b" / "opencode.db"
    write_opencode_session(db_a, "ses_aaa", "/a/work")
    write_opencode_session(db_b, "ses_bbb", "/b/work")
    session_id, source_db = resolve_adopt_session_db("ses_bbb", [db_a, db_b])
    assert session_id == "ses_bbb"
    assert source_db == db_b


def test_resolve_adopt_session_db_by_id_missing_raises(tmp_path: Path) -> None:
    db_a = tmp_path / "a" / "opencode.db"
    write_opencode_session(db_a, "ses_aaa", "/a/work")
    with pytest.raises(UserInputError, match="not found"):
        resolve_adopt_session_db("ses_zzz", [db_a])


def test_resolve_adopt_session_db_by_id_ambiguous_raises(tmp_path: Path) -> None:
    db_a = tmp_path / "a" / "opencode.db"
    db_b = tmp_path / "b" / "opencode.db"
    write_opencode_session(db_a, "ses_dup", "/a/work")
    write_opencode_session(db_b, "ses_dup", "/b/work")
    with pytest.raises(UserInputError, match="multiple stores"):
        resolve_adopt_session_db("ses_dup", [db_a, db_b])


def test_collect_adopt_search_db_paths_includes_agent_and_preserved_dbs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_dir = tmp_path / "host"
    live_db = host_dir / "agents" / "agent-1" / AGENT_OPENCODE_DB_RELPATH
    preserved_db = host_dir / "preserved" / "name--id" / AGENT_OPENCODE_DB_RELPATH
    write_opencode_session(live_db, "ses_live", "/live/work")
    write_opencode_session(preserved_db, "ses_preserved", "/preserved/work")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    agent_db_paths = iter_agent_session_paths(host_dir, AGENT_OPENCODE_DB_RELPATH)
    paths = collect_adopt_search_db_paths(agent_db_paths)
    assert live_db in paths
    assert preserved_db in paths
    # The user-native db path is always included (even when it does not exist on disk).
    assert (tmp_path / "user-data" / "opencode" / "opencode.db") in paths


def test_build_opencode_rebind_sql_rewrites_every_stored_source_worktree_path(tmp_path: Path) -> None:
    """The rebind SQL rewrites the session, project, and project_directory paths to the new dir.

    Applied via the stdlib ``sqlite3`` module (the same engine the host ``sqlite3`` CLI runs)
    so the test does not depend on the CLI being installed.
    """
    db_path = tmp_path / "opencode.db"
    write_opencode_session(db_path, "ses_root", "/old/src/work")
    new_directory = tmp_path / "new" / "work"
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(build_opencode_rebind_sql("ses_root", new_directory))
        connection.commit()
        assert connection.execute("SELECT directory FROM session WHERE id='ses_root'").fetchone()[0] == str(
            new_directory
        )
        assert connection.execute("SELECT worktree FROM project").fetchone()[0] == str(new_directory)
        directories = {row[0] for row in connection.execute("SELECT directory FROM project_directory").fetchall()}
        assert str(new_directory) in directories
    finally:
        connection.close()


def test_apply_opencode_rebind_checkpoints_and_rewrites_paths(tmp_path: Path) -> None:
    """``apply_opencode_rebind`` rewrites the session/project/project_directory paths in a local db."""
    db_path = tmp_path / "opencode.db"
    write_opencode_session(db_path, "ses_root", "/old/src/work")
    new_directory = tmp_path / "new" / "work"
    apply_opencode_rebind(db_path, "ses_root", new_directory)
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT directory FROM session WHERE id='ses_root'").fetchone()[0] == str(
            new_directory
        )
        assert connection.execute("SELECT worktree FROM project").fetchone()[0] == str(new_directory)
        directories = {row[0] for row in connection.execute("SELECT directory FROM project_directory").fetchall()}
        assert str(new_directory) in directories
    finally:
        connection.close()


def _apply_merge_sql(dest_db: Path, source_db: Path, session_id: str) -> None:
    """Apply the merge via the lib (stdlib sqlite3 module), folding ``session_id`` from source into dest."""
    apply_opencode_merge(dest_db, source_db, session_id)


def test_build_opencode_merge_sql_folds_session_and_dependents_keeping_existing(tmp_path: Path) -> None:
    """Merging a source session adds its full row set without disturbing the dest's existing session."""
    dest_db = tmp_path / "dest.db"
    source_db = tmp_path / "src.db"
    write_opencode_session(dest_db, "ses_a", "/dest/work", message_id="msg_a")
    write_opencode_session(source_db, "ses_b", "/src/work", message_id="msg_b")
    _apply_merge_sql(dest_db, source_db, "ses_b")
    connection = sqlite3.connect(dest_db)
    try:
        sessions = {row[0] for row in connection.execute("SELECT id FROM session").fetchall()}
        messages = {row[0] for row in connection.execute("SELECT id FROM message").fetchall()}
        parts = {row[0] for row in connection.execute("SELECT id FROM part").fetchall()}
        projects = {row[0] for row in connection.execute("SELECT id FROM project").fetchall()}
    finally:
        connection.close()
    assert sessions == {"ses_a", "ses_b"}
    assert messages == {"msg_a", "msg_b"}
    assert parts == {"prt_msg_a", "prt_msg_b"}
    assert projects == {"proj_ses_a", "proj_ses_b"}


def test_build_opencode_merge_sql_carries_descendant_subagent_sessions(tmp_path: Path) -> None:
    """Merging a root session brings its child (subagent) sessions and their rows along."""
    dest_db = tmp_path / "dest.db"
    source_db = tmp_path / "src.db"
    write_opencode_session(dest_db, "ses_a", "/dest/work")
    write_opencode_session(source_db, "ses_root", "/src/work", message_id="msg_root")
    write_opencode_session(source_db, "ses_child", "/src/work", parent_id="ses_root", message_id="msg_child")
    _apply_merge_sql(dest_db, source_db, "ses_root")
    connection = sqlite3.connect(dest_db)
    try:
        sessions = {row[0] for row in connection.execute("SELECT id FROM session").fetchall()}
        messages = {row[0] for row in connection.execute("SELECT id FROM message").fetchall()}
    finally:
        connection.close()
    assert sessions == {"ses_a", "ses_root", "ses_child"}
    assert messages == {"msg_root", "msg_child"}


def test_build_opencode_merge_sql_never_copies_global_migration_rows(tmp_path: Path) -> None:
    """The merge copies session/project-scoped rows only, never the global ``migration`` table."""
    dest_db = tmp_path / "dest.db"
    source_db = tmp_path / "src.db"
    write_opencode_session(dest_db, "ses_a", "/dest/work")
    write_opencode_session(source_db, "ses_b", "/src/work")
    for db_path, mig_id in ((dest_db, "mig_dest"), (source_db, "mig_src")):
        connection = sqlite3.connect(db_path)
        connection.execute("INSERT INTO migration (id, time_completed) VALUES (?, 1)", (mig_id,))
        connection.commit()
        connection.close()
    _apply_merge_sql(dest_db, source_db, "ses_b")
    connection = sqlite3.connect(dest_db)
    try:
        migrations = {row[0] for row in connection.execute("SELECT id FROM migration").fetchall()}
    finally:
        connection.close()
    assert migrations == {"mig_dest"}


def test_build_opencode_merge_sql_is_idempotent(tmp_path: Path) -> None:
    """Re-merging the same session is a no-op (``INSERT OR IGNORE`` skips already-present rows)."""
    dest_db = tmp_path / "dest.db"
    source_db = tmp_path / "src.db"
    write_opencode_session(dest_db, "ses_a", "/dest/work")
    write_opencode_session(source_db, "ses_b", "/src/work", message_id="msg_b")
    _apply_merge_sql(dest_db, source_db, "ses_b")
    _apply_merge_sql(dest_db, source_db, "ses_b")
    connection = sqlite3.connect(dest_db)
    try:
        message_count = connection.execute("SELECT COUNT(*) FROM message WHERE id='msg_b'").fetchone()[0]
    finally:
        connection.close()
    assert message_count == 1


def test_list_source_merge_tables_reports_only_present_tables(tmp_path: Path) -> None:
    """A source missing ``project_directory`` (some opencode versions) is reported without it."""
    source_db = tmp_path / "src.db"
    write_opencode_session(source_db, "ses_b", "/src/work")
    assert "project_directory" in list_source_merge_tables(source_db)
    connection = sqlite3.connect(source_db)
    connection.execute("DROP TABLE project_directory")
    connection.commit()
    connection.close()
    tables = list_source_merge_tables(source_db)
    assert "project_directory" not in tables
    assert "session" in tables and "message" in tables
