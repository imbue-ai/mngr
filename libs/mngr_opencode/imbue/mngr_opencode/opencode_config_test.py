"""Unit tests for the pure opencode_config builders, readers, and path helpers."""

import importlib.resources
import json
import sqlite3
import subprocess
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
from imbue.mngr_opencode.opencode_config import build_opencode_config
from imbue.mngr_opencode.opencode_config import build_opencode_rebind_commands
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
from imbue.mngr_opencode.opencode_config import read_opencode_config
from imbue.mngr_opencode.opencode_config import resolve_adopt_session_db
from imbue.mngr_opencode.opencode_config import serialize_opencode_config


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


def _write_opencode_db(db_path: Path, session_id: str, directory: str, *, parent_id: str | None = None) -> str:
    """Create a minimal OpenCode-shaped db with one project + one session; return the project id.

    Mirrors only the columns the adopt resolver / rebind touch (verified against the real
    opencode 1.17.7 schema): session.(id, project_id, parent_id, directory), project.(id,
    worktree), project_directory.(project_id, directory).
    """
    project_id = f"proj_{session_id}"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            "CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT, directory TEXT NOT NULL);"
            "CREATE TABLE project (id TEXT PRIMARY KEY, worktree TEXT NOT NULL);"
            "CREATE TABLE project_directory (project_id TEXT NOT NULL, directory TEXT NOT NULL, time_created INTEGER, "
            "PRIMARY KEY (project_id, directory));"
        )
        connection.execute(
            "INSERT INTO session (id, project_id, parent_id, directory) VALUES (?, ?, ?, ?)",
            (session_id, project_id, parent_id, directory),
        )
        connection.execute("INSERT INTO project (id, worktree) VALUES (?, ?)", (project_id, directory))
        connection.execute(
            "INSERT INTO project_directory (project_id, directory, time_created) VALUES (?, ?, 0)",
            (project_id, directory),
        )
        connection.commit()
    finally:
        connection.close()
    return project_id


def test_resolve_adopt_session_db_by_path_uses_lone_root_session(tmp_path: Path) -> None:
    db_path = tmp_path / "opencode.db"
    _write_opencode_db(db_path, "ses_root", "/src/work")
    session_id, source_db = resolve_adopt_session_db(str(db_path), [])
    assert session_id == "ses_root"
    assert source_db == db_path


def test_resolve_adopt_session_db_by_path_rejects_multiple_roots(tmp_path: Path) -> None:
    db_path = tmp_path / "opencode.db"
    _write_opencode_db(db_path, "ses_root", "/src/work")
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
    _write_opencode_db(db_a, "ses_aaa", "/a/work")
    _write_opencode_db(db_b, "ses_bbb", "/b/work")
    session_id, source_db = resolve_adopt_session_db("ses_bbb", [db_a, db_b])
    assert session_id == "ses_bbb"
    assert source_db == db_b


def test_resolve_adopt_session_db_by_id_missing_raises(tmp_path: Path) -> None:
    db_a = tmp_path / "a" / "opencode.db"
    _write_opencode_db(db_a, "ses_aaa", "/a/work")
    with pytest.raises(UserInputError, match="not found"):
        resolve_adopt_session_db("ses_zzz", [db_a])


def test_resolve_adopt_session_db_by_id_ambiguous_raises(tmp_path: Path) -> None:
    db_a = tmp_path / "a" / "opencode.db"
    db_b = tmp_path / "b" / "opencode.db"
    _write_opencode_db(db_a, "ses_dup", "/a/work")
    _write_opencode_db(db_b, "ses_dup", "/b/work")
    with pytest.raises(UserInputError, match="multiple stores"):
        resolve_adopt_session_db("ses_dup", [db_a, db_b])


def test_collect_adopt_search_db_paths_includes_agent_and_preserved_dbs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_dir = tmp_path / "host"
    live_db = host_dir / "agents" / "agent-1" / AGENT_OPENCODE_DB_RELPATH
    preserved_db = host_dir / "preserved" / "name--id" / AGENT_OPENCODE_DB_RELPATH
    _write_opencode_db(live_db, "ses_live", "/live/work")
    _write_opencode_db(preserved_db, "ses_preserved", "/preserved/work")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    agent_db_paths = iter_agent_session_paths(host_dir, AGENT_OPENCODE_DB_RELPATH)
    paths = collect_adopt_search_db_paths(agent_db_paths)
    assert live_db in paths
    assert preserved_db in paths
    # The user-native db path is always included (even when it does not exist on disk).
    assert (tmp_path / "user-data" / "opencode" / "opencode.db") in paths


def test_build_opencode_rebind_commands_actually_rebinds(tmp_path: Path) -> None:
    """The emitted sqlite3 commands rewrite every stored source-worktree path to the new dir."""
    db_path = tmp_path / "opencode.db"
    _write_opencode_db(db_path, "ses_root", "/old/src/work")
    new_directory = tmp_path / "new" / "work"
    for command in build_opencode_rebind_commands(db_path, "ses_root", new_directory):
        subprocess.run(command, shell=True, check=True, capture_output=True)
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
