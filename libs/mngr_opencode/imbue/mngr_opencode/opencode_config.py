"""Pure config/path helpers for running OpenCode under a per-agent config + data dir.

OpenCode (https://opencode.ai) isolates cleanly via two env vars (the *preferred*
config-dir shape, so -- unlike ``mngr_antigravity`` -- there is no ``$HOME``
relocation):

* ``OPENCODE_CONFIG_DIR`` -- the config dir holding ``opencode.json`` and the
  auto-loaded ``plugin/*.ts``. Pointed at a per-agent dir so each agent has its
  own model/permission policy and its own copy of the lifecycle plugin.
* ``XDG_DATA_HOME`` -- the data root under which OpenCode keeps
  ``opencode/{opencode.db,auth.json,storage,log}``. Pointed at a per-agent dir so
  sessions (and therefore resume) and credentials are per-agent. (OpenCode reads
  ``$XDG_DATA_HOME/opencode``; ``OPENCODE_CONFIG_DIR`` moves *only* config, not
  data -- the two are independent.)

Both are injected only on the OpenCode process (via an ``env`` prefix in the
plugin's ``assemble_command``), so tmux and the transcript supervisor keep the
real environment.

This module holds the host-agnostic pieces of that scheme: path builders that
take the agent state dir, the ``opencode.json`` body builder/serializer/reader,
and the filename constants the in-process plugin (``resources/``) and the
common-transcript converter share. The plugin and converter hardcode the same
literals (they run as standalone TS/Python and cannot import this module); the
``*_FILENAME`` / ``*_RELATIVE_PATH`` constants here are the single source of
truth those resources are kept in sync with.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import OnlineHostInterface

# Per-agent config dir (-> OPENCODE_CONFIG_DIR), under the agent state dir. Holds
# opencode.json and plugin/<the lifecycle plugin>. OpenCode reads opencode.json
# directly from this dir and auto-loads plugin/*.ts from it (verified live; no
# config ``plugin`` entry needed).
_CONFIG_DIR_RELATIVE_PATH: tuple[str, ...] = ("plugin", "opencode", "config")

# Per-agent data root (-> XDG_DATA_HOME), under the agent state dir. OpenCode
# keeps its db/auth/storage/logs under ``<this>/opencode``.
_DATA_HOME_RELATIVE_PATH: tuple[str, ...] = ("plugin", "opencode", "data")

# OpenCode namespaces everything it writes under ``$XDG_DATA_HOME/opencode``.
_OPENCODE_APP_DIR_NAME: str = "opencode"
_AUTH_FILENAME: str = "auth.json"
_DB_FILENAME: str = "opencode.db"
_STORAGE_DIR_NAME: str = "storage"
_CONFIG_FILENAME: str = "opencode.json"
_PLUGIN_DIR_NAME: str = "plugin"

# The lifecycle plugin file dropped into the per-agent ``<config dir>/plugin/``.
# Auto-loaded by OpenCode; maintains the active marker and writes the raw
# transcript (see ``resources/mngr_opencode_plugin.ts``).
PLUGIN_FILENAME: str = "mngr_opencode_plugin.ts"

# Marker file (in ``$MNGR_AGENT_STATE_DIR``) whose presence
# ``BaseAgent.get_lifecycle_state`` reads as RUNNING; absence means WAITING. The
# plugin touches/removes it. Kept in sync with the literal ``"active"`` core checks.
ACTIVE_MARKER_FILENAME: str = "active"

# Marker file (in ``$MNGR_AGENT_STATE_DIR``) present while opencode is blocked on a
# tool-approval prompt (its ``ask`` permission policy). The lifecycle plugin touches
# it while one or more permissions are pending and removes it once they are all
# answered; ``OpenCodeAgent.get_lifecycle_state`` promotes RUNNING -> WAITING while
# it is present, and ``_waiting_reason`` reports ``PERMISSIONS``. The plugin
# hardcodes this same literal; keep the two in sync.
PERMISSIONS_WAITING_FILENAME: str = "permissions_waiting"

# Per-agent file (in ``$MNGR_AGENT_STATE_DIR``) recording the *root* OpenCode
# session id. ``opencode_launch.sh`` creates the session (via the server API)
# and writes its id here on first launch, then reuses it on every restart so the
# attached TUI resumes the same conversation; ``send_message`` reads it to know
# which session to POST to. The launch script hardcodes this same literal.
ROOT_SESSION_FILENAME: str = "opencode_root_session"

# Per-agent file (in ``$MNGR_AGENT_STATE_DIR``) recording the TCP port the
# agent's ``opencode serve`` bound. ``opencode_launch.sh`` writes it (parsed from
# the server's "listening on" line); ``send_message`` reads it to POST prompts to
# the right server. The launch script hardcodes this same literal.
SERVER_PORT_FILENAME: str = "opencode_server_port"

# Readiness sentinel (in ``$MNGR_AGENT_STATE_DIR``). ``opencode_launch.sh`` clears
# it at startup and writes it once the server is up and the session exists -- i.e.
# the agent can accept messages. ``wait_for_ready_signal`` polls for it instead of
# scraping the TUI footer. The launch script hardcodes this same literal.
READY_SENTINEL_FILENAME: str = "opencode_ready"

# The launch orchestrator provisioned into ``commands/``: starts ``opencode
# serve`` + pre-creates/reuses the session + attaches the TUI (see the resource).
LAUNCH_SCRIPT_NAME: str = "opencode_launch.sh"

# Env var that marks the single process allowed to maintain the marker/transcript
# (the ``serve`` process). The launch script sets it only on ``serve``; the plugin
# checks it so the attach client stays inert. Both resources hardcode these.
ROLE_ENV_VAR: str = "MNGR_OPENCODE_ROLE"
SERVER_ROLE: str = "server"

# Env var (set on the OpenCode process by ``assemble_command`` when
# ``emit_common_transcript`` is enabled) that tells the in-process plugin to emit
# the common transcript on session idle. The plugin hardcodes the name + ``"1"``.
EMIT_COMMON_ENV_VAR: str = "MNGR_OPENCODE_EMIT_COMMON"
EMIT_COMMON_ENABLED_VALUE: str = "1"

# Env vars the launch script reads (set by ``assemble_command``). The port is
# passed as ``0`` so ``opencode serve`` binds an OS-assigned free port; the launch
# script records the actual bound port (co-resident agents never collide). The
# workdir is passed already URL-encoded (mngr encodes it in Python) because the
# script drops it straight into the session-create ``?directory=`` query.
OPENCODE_BIN_ENV_VAR: str = "MNGR_OPENCODE_BIN"
OPENCODE_PORT_ENV_VAR: str = "MNGR_OPENCODE_PORT"
OPENCODE_WORKDIR_ENV_VAR: str = "MNGR_OPENCODE_WORKDIR"

# Raw transcript path (relative to ``$MNGR_AGENT_STATE_DIR``). The plugin appends
# each ``message.updated`` / ``message.part.updated`` event here verbatim; the
# converter reads it. Mirrors the ``logs/<type>_transcript/events.jsonl`` layout
# the transcript mixins document. The plugin hardcodes this same literal.
RAW_TRANSCRIPT_RELATIVE_PATH: str = "logs/opencode_transcript/events.jsonl"

# Common-transcript path (relative to ``$MNGR_AGENT_STATE_DIR``) that ``mngr
# transcript`` reads. The converter writes it. The converter hardcodes this literal.
COMMON_TRANSCRIPT_RELATIVE_PATH: str = "events/opencode/common_transcript/events.jsonl"

# ``source`` stamped on every common-transcript event (mirrors agy's scheme).
COMMON_TRANSCRIPT_SOURCE: str = "opencode/common_transcript"

# OpenCode's native resumable session store (relative to ``$MNGR_AGENT_STATE_DIR``,
# POSIX strings), preserved on destroy so the session can be resumed/adopted. These
# target the db file + storage dir specifically; the sibling ``auth.json`` (a symlink
# to shared creds) and ``log/`` under the same ``opencode/`` dir are deliberately excluded.
# The db is SQLite in WAL mode: the ``-wal`` (and ``-shm``) sidecars hold writes not yet
# checkpointed into the main file, so they must be preserved alongside it or the most
# recent turns are lost. Both are absent once checkpointed (e.g. a clean shutdown), so
# preservation skips them when missing.
NATIVE_DB_RELATIVE_PATH: str = "/".join((*_DATA_HOME_RELATIVE_PATH, _OPENCODE_APP_DIR_NAME, _DB_FILENAME))
NATIVE_DB_WAL_RELATIVE_PATH: str = f"{NATIVE_DB_RELATIVE_PATH}-wal"
NATIVE_DB_SHM_RELATIVE_PATH: str = f"{NATIVE_DB_RELATIVE_PATH}-shm"
# ``storage/`` is a pre-SQLite-migration layout: on current opencode (verified 1.17.7) all
# conversation content lives in the db, and ``storage/`` is empty -- so preserving it is a
# no-op there. Kept (and skipped when absent) for back-compat with older opencode versions
# that did file the message parts under it.
NATIVE_STORAGE_RELATIVE_PATH: str = "/".join((*_DATA_HOME_RELATIVE_PATH, _OPENCODE_APP_DIR_NAME, _STORAGE_DIR_NAME))


def get_opencode_root_session_file_path(agent_state_dir: Path) -> Path:
    """Return the file recording the agent's root OpenCode session id."""
    return agent_state_dir / ROOT_SESSION_FILENAME


def get_opencode_server_port_file_path(agent_state_dir: Path) -> Path:
    """Return the file recording the port the agent's OpenCode server bound."""
    return agent_state_dir / SERVER_PORT_FILENAME


def get_opencode_config_dir(agent_state_dir: Path) -> Path:
    """Return the per-agent OpenCode config dir (the ``OPENCODE_CONFIG_DIR`` value)."""
    return agent_state_dir.joinpath(*_CONFIG_DIR_RELATIVE_PATH)


def get_opencode_data_home(agent_state_dir: Path) -> Path:
    """Return the per-agent OpenCode data root (the ``XDG_DATA_HOME`` value)."""
    return agent_state_dir.joinpath(*_DATA_HOME_RELATIVE_PATH)


def get_opencode_app_data_dir(data_home: Path) -> Path:
    """Return ``<data_home>/opencode`` -- where OpenCode keeps db/auth/storage/logs."""
    return data_home / _OPENCODE_APP_DIR_NAME


def get_opencode_config_file_path(config_dir: Path) -> Path:
    """Return the ``opencode.json`` path under an OpenCode config dir."""
    return config_dir / _CONFIG_FILENAME


def get_opencode_plugin_path(config_dir: Path) -> Path:
    """Return the lifecycle-plugin path under an OpenCode config dir's ``plugin/``."""
    return config_dir / _PLUGIN_DIR_NAME / PLUGIN_FILENAME


def get_opencode_auth_path_for_data_home(data_home: Path) -> Path:
    """Return the ``auth.json`` path under a ``XDG_DATA_HOME`` root."""
    return get_opencode_app_data_dir(data_home) / _AUTH_FILENAME


def get_shared_opencode_auth_path(host_home: Path) -> Path:
    """Return the user's shared ``auth.json`` at the default ``~/.local/share`` data dir.

    This is the login-once target the per-agent ``auth.json`` symlinks to when
    ``symlink_auth`` is set (OpenCode writes ``auth.json`` in place, so a login in
    any agent writes through to this shared file and authenticates the rest).
    """
    return host_home / ".local" / "share" / _OPENCODE_APP_DIR_NAME / _AUTH_FILENAME


def read_opencode_config(host: OnlineHostInterface, config_path: Path) -> dict[str, Any]:
    """Read an ``opencode.json`` via the host filesystem, validating its shape.

    A missing or empty file yields an empty dict (clean fall-through to a fresh
    write). Malformed JSON, or a valid document whose top-level value is not an
    object, raises ``UserInputError`` rather than being silently treated as
    empty: the file is user-tier state OpenCode reads at every launch, and
    overwriting hand-edited content would be data loss. Mirrors
    ``mngr_antigravity``'s ``read_antigravity_settings``.
    """
    try:
        content = host.read_text_file(config_path)
    except FileNotFoundError:
        return {}
    if not content.strip():
        return {}
    try:
        parsed: Any = json.loads(content)
    except json.JSONDecodeError as exc:
        raise UserInputError(
            f"OpenCode config at {config_path} contains malformed JSON ({exc}); "
            f"refusing to overwrite. Inspect the file by hand and either fix it or remove it, "
            f"then re-run."
        ) from exc
    if not isinstance(parsed, dict):
        raise UserInputError(
            f"OpenCode config at {config_path} has a non-object top-level value "
            f"({type(parsed).__name__}); refusing to overwrite. Inspect the file by hand "
            f"and either fix it or remove it, then re-run."
        )
    return parsed


# OpenCode config keys. ``permission`` (singular) is the policy block; a value of
# ``"allow"`` for the ``*`` glob auto-approves every action not explicitly denied.
_PERMISSION_KEY: str = "permission"
_SCHEMA_KEY: str = "$schema"
_SCHEMA_URL: str = "https://opencode.ai/config.json"
_PERMISSION_WILDCARD: str = "*"
_PERMISSION_ALLOW: str = "allow"
# ``autoupdate`` governs opencode's startup self-update; ``false`` disables it so the
# installed (possibly pinned) version stays put.
_AUTOUPDATE_KEY: str = "autoupdate"


@pure
def build_opencode_config(
    base_config: Mapping[str, Any],
    config_overrides: Mapping[str, Any],
    is_auto_allow_permissions: bool,
    disable_auto_update: bool = False,
) -> dict[str, Any]:
    """Build a per-agent ``opencode.json`` body by layering (low -> high precedence).

    1. ``base_config`` -- a copy of the user's real ``opencode.json`` (when
       ``sync_global_config``) or an empty dict. Copied, never mutated. Carries
       the user's model/provider/theme defaults so the agent starts usable.
    2. A wildcard allow ``permission`` block when ``is_auto_allow_permissions`` --
       auto-approves every action not explicitly denied (the config analog of
       ``--dangerously-skip-permissions``; a finer policy instead comes via
       ``config_overrides``).
    3. ``"autoupdate": false`` when ``disable_auto_update`` -- set before
       ``config_overrides`` so an explicit ``autoupdate`` override still wins.
    4. ``config_overrides`` -- the per-agent-type blob (``model``, ``permission``,
       ...), applied last so it wins.

    A ``$schema`` is always set so the file validates and editors autocomplete.
    """
    config: dict[str, Any] = dict(base_config)
    config[_SCHEMA_KEY] = _SCHEMA_URL
    if is_auto_allow_permissions:
        config[_PERMISSION_KEY] = {_PERMISSION_WILDCARD: _PERMISSION_ALLOW}
    if disable_auto_update:
        config[_AUTOUPDATE_KEY] = False
    config.update(config_overrides)
    return config


@pure
def serialize_opencode_config(config: Mapping[str, Any]) -> str:
    """Serialize an ``opencode.json`` body as two-space-indented JSON."""
    return json.dumps(dict(config), indent=2)


# The agent's native opencode store dir (the dir holding ``opencode.db`` + its ``-wal``/``-shm``
# sidecars), relative to its state dir (under the per-agent ``XDG_DATA_HOME``). A ``--from`` clone
# transfers exactly this dir from the source agent's state dir into its own.
AGENT_OPENCODE_STORE_RELPATH: Final[Path] = Path(*_DATA_HOME_RELATIVE_PATH, _OPENCODE_APP_DIR_NAME)

# The agent's native ``opencode.db`` relative to its state dir. The plugin passes this to the shared
# live/preserved agent scanner (``iter_agent_session_paths``) to find every local agent's db; kept
# here so the path layout stays defined alongside the other opencode-data constants.
AGENT_OPENCODE_DB_RELPATH: Final[Path] = AGENT_OPENCODE_STORE_RELPATH / _DB_FILENAME

# Where a user-native (plain-CLI) OpenCode install keeps its data, relative to the data
# home root resolved from ``$XDG_DATA_HOME`` (or ``~/.local/share``).
_USER_DATA_HOME_PARTS: Final[tuple[str, ...]] = (".local", "share")


def get_opencode_db_path_for_data_home(data_home: Path) -> Path:
    """Return the ``opencode.db`` path under a ``XDG_DATA_HOME`` root."""
    return get_opencode_app_data_dir(data_home) / _DB_FILENAME


def get_user_native_opencode_db_path() -> Path:
    """Return the user-native ``opencode.db`` (plain-CLI install), honoring ``$XDG_DATA_HOME``."""
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    data_home = Path(xdg_data_home) if xdg_data_home else Path.home().joinpath(*_USER_DATA_HOME_PARTS)
    return get_opencode_db_path_for_data_home(data_home)


def collect_adopt_search_db_paths(agent_db_paths: Sequence[Path]) -> list[Path]:
    """Return the ``opencode.db`` paths an adopt session id is searched across (local only).

    The user-native db (plain-CLI install) plus ``agent_db_paths`` -- every live local mngr
    agent's and preserved agent's db, which the plugin gathers via the shared
    ``iter_agent_session_paths`` scanner. Local sources only: the resolved db is copied onto
    the destination host from a path reachable as a local source.
    """
    return [get_user_native_opencode_db_path(), *agent_db_paths]


def _db_has_session(db_path: Path, session_id: str) -> bool:
    """Return whether ``db_path`` (an OpenCode SQLite db) contains a session with ``session_id``.

    Opened read-only so a live agent's db is never disturbed; a malformed/locked db is
    treated as "no match" so one bad store can't block resolving a session that lives
    elsewhere.
    """
    try:
        # Connect failures are dominated by the benign "file does not exist" case, so not logged.
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        cursor = connection.execute("SELECT 1 FROM session WHERE id = ? LIMIT 1", (session_id,))
        return cursor.fetchone() is not None
    except sqlite3.Error as exc:
        # File exists but is malformed/corrupt: a real anomaly worth surfacing, but still treated as
        # no match so resolution continues against the other stores.
        logger.warning("Could not query sessions in OpenCode db {} (treated as no match): {}", db_path, exc)
        return False
    finally:
        connection.close()


def resolve_adopt_session_db(adopt_session_arg: str, search_db_paths: Sequence[Path]) -> tuple[str, Path]:
    """Resolve an adopt argument to a ``(session_id, source_db_path)`` pair.

    Accepts either an absolute path to a source ``opencode.db`` (its single root session is
    used), or a ``ses_...`` session id searched across ``search_db_paths`` (user-native +
    every live and preserved mngr agent's db). A session id found in more than one db is
    rejected as ambiguous (mirrors the claude adopt resolver), so the caller must pass the
    db path instead.
    """
    if adopt_session_arg.endswith(".db"):
        source_db = Path(adopt_session_arg).resolve()
        if not source_db.is_file():
            raise UserInputError(f"OpenCode session db not found: {source_db}")
        return read_only_root_session_id(source_db), source_db

    matches = [db_path for db_path in search_db_paths if _db_has_session(db_path, adopt_session_arg)]
    if not matches:
        raise UserInputError(
            f"OpenCode session {adopt_session_arg} not found in any live, preserved, or user-native "
            "store. Check that the session id is correct, or pass a path to the source opencode.db."
        )
    if len(matches) > 1:
        match_list = "\n".join(f"  {match}" for match in matches)
        raise UserInputError(
            f"OpenCode session {adopt_session_arg} found in multiple stores:\n{match_list}\n"
            "Pass the full path to the source opencode.db to specify which one."
        )
    return adopt_session_arg, matches[0]


def read_only_root_session_id(db_path: Path) -> str:
    """Return the lone root (parent-less) session id in ``db_path``, opened read-only.

    Adoption resumes one root conversation; a db with zero or several roots is ambiguous
    when addressed by path, so require exactly one.
    """
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT id FROM session WHERE parent_id IS NULL").fetchall()
    except sqlite3.Error as exc:
        raise UserInputError(f"Could not read sessions from OpenCode db {db_path}: {exc}") from exc
    finally:
        connection.close()
    if len(rows) != 1:
        raise UserInputError(
            f"OpenCode db {db_path} has {len(rows)} root sessions; expected exactly one. "
            "Pass a session id instead of a db path to disambiguate."
        )
    return str(rows[0][0])


# Fold the ``-wal``/``-shm`` sidecars into the main db so the rebind sees and rewrites the
# committed rows. Run before the rebind script.
_WAL_CHECKPOINT_SQL: Final[str] = "PRAGMA wal_checkpoint(TRUNCATE);"


def build_opencode_rebind_sql(session_id: str, new_directory: Path) -> str:
    """Build the SQL script that rebinds an adopted session's stored source-worktree paths.

    OpenCode stores the absolute source worktree on the ``session`` row, the owning ``project``
    row, and a ``project_directory`` row; after copying the db into the new agent these all
    still point at the destroyed source worktree, so the session must be rebound to the new
    agent's work dir or recall silently no-ops against it. The ``project_directory`` upsert
    leaves any pre-existing row in place (its PK is ``(project_id, directory)``, so the new row
    is additive) -- harmless, and it matches how OpenCode records additional directories.
    Verified live against opencode 1.17.7.
    """
    quoted_session = _sqlite_quote(session_id)
    quoted_dir = _sqlite_quote(str(new_directory))
    return (
        f"UPDATE session SET directory = {quoted_dir} WHERE id = {quoted_session};"
        f"UPDATE project SET worktree = {quoted_dir} "
        f"WHERE id = (SELECT project_id FROM session WHERE id = {quoted_session});"
        f"INSERT INTO project_directory (project_id, directory, time_created) "
        f"SELECT project_id, {quoted_dir}, CAST(strftime('%s','now') AS INTEGER) * 1000 "
        f"FROM session WHERE id = {quoted_session} "
        f"ON CONFLICT (project_id, directory) DO NOTHING;"
    )


def apply_opencode_rebind(db_path: Path, session_id: str, new_directory: Path) -> None:
    """Checkpoint then rebind ``session_id``'s stored worktree paths in ``db_path`` to ``new_directory``.

    Applied to a LOCAL staging db via the stdlib ``sqlite3`` module (not the host ``sqlite3`` CLI), so
    no CLI dependency on the destination host. The WAL checkpoint folds the ``-wal``/``-shm`` sidecars
    into the main file first, so a subsequent file copy to the host carries the rebound rows.
    """
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(_WAL_CHECKPOINT_SQL)
        connection.executescript(build_opencode_rebind_sql(session_id, new_directory))
        connection.commit()
    finally:
        connection.close()


# Tables whose rows are scoped to a session (directly or through the session's owning project), in
# FK-dependency order (parents before children) so a copy that ever runs with foreign keys enabled
# still satisfies the references. ``project``/``permission`` are project-scoped; the rest are
# session-scoped. ``__drizzle_migrations``/``migration``/``control_account`` are global (schema and
# auth state) and deliberately excluded -- merging them would duplicate migration bookkeeping or
# clobber the dest agent's own account rows. Verified against the opencode 1.17.7 schema.
_PROJECT_SCOPED_MERGE_TABLES: Final[tuple[str, ...]] = ("project", "permission")
_SESSION_SCOPED_MERGE_TABLES: Final[tuple[str, ...]] = (
    "session",
    "message",
    "part",
    "todo",
    "session_share",
)
# ``project_directory`` is project-scoped but absent on some opencode versions (the installed 1.17.7
# db lacks it while the rebind still upserts into it), so it is merged only when the source has it.
_OPTIONAL_PROJECT_SCOPED_MERGE_TABLE: Final[str] = "project_directory"

# All tables the merge may touch; used to detect which actually exist in a given source db.
_ALL_MERGE_TABLES: Final[tuple[str, ...]] = (
    *_PROJECT_SCOPED_MERGE_TABLES,
    _OPTIONAL_PROJECT_SCOPED_MERGE_TABLE,
    *_SESSION_SCOPED_MERGE_TABLES,
)

# Column the project-scoped tables key off (``project`` itself keys off ``id``).
_PROJECT_KEY_BY_TABLE: Final[Mapping[str, str]] = {"project": "id"}

# Column the session-scoped tables key off (the ``session`` row itself keys off its ``id``).
_SESSION_KEY_BY_TABLE: Final[Mapping[str, str]] = {"session": "id"}


def list_source_merge_tables(source_db: Path) -> tuple[str, ...]:
    """Return which session/project-scoped tables exist in ``source_db`` (opened read-only).

    The source is always a local opencode db, so this is read with the stdlib ``sqlite3`` module.
    Used to build a merge script that touches only tables the source actually has (e.g. older/newer
    opencode versions differ on ``project_directory``), so the merge never fails on an absent table.
    """
    connection = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    try:
        present = {
            str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    finally:
        connection.close()
    return tuple(table for table in _ALL_MERGE_TABLES if table in present)


def build_opencode_merge_sql(staged_source_db: Path, session_id: str, present_tables: Sequence[str]) -> str:
    """Build the SQL that merges one session's rows from ``staged_source_db`` into the open db.

    Applied via :func:`apply_opencode_merge` when a *subsequent* ``--adopt`` session must be folded
    into a staging ``opencode.db`` that already holds an earlier adopted session (the single-file
    store means later sessions are merged in rather than copied as a fresh db). It attaches the staged
    source copy and, for the adopted session plus all its descendant (sub-)sessions, copies the owning
    ``project`` (and ``permission``/``project_directory``) rows and every session-scoped row
    (``session``/``message``/``part``/``todo``/``session_share``). ``INSERT OR IGNORE`` makes a shared
    project or an already-present row a harmless no-op, so re-merging is idempotent. ``present_tables``
    (from :func:`list_source_merge_tables`) bounds the copy to tables the source actually has.

    The descendant walk follows ``session.parent_id`` so a resumed root brings its subagent sessions;
    rows are copied in FK-dependency order. The session set / project set live in temp tables so the
    recursive walk runs once. ``staged_source_db`` is the source db *as a local path* (it must be the
    full trio -- db + ``-wal``/``-shm`` sidecars -- so the attach sees uncheckpointed writes).
    """
    quoted_source = _sqlite_quote(str(staged_source_db))
    quoted_session = _sqlite_quote(session_id)
    statements: list[str] = [
        f"ATTACH DATABASE {quoted_source} AS src;",
        # Adopted session + all descendant (sub-)sessions, walked via parent_id.
        "CREATE TEMP TABLE _adopt_sessions AS "
        "WITH RECURSIVE descendants(id) AS ("
        f"SELECT id FROM src.session WHERE id = {quoted_session} "
        "UNION "
        "SELECT s.id FROM src.session s JOIN descendants d ON s.parent_id = d.id"
        ") SELECT id FROM descendants;",
        "CREATE TEMP TABLE _adopt_projects AS "
        "SELECT DISTINCT project_id AS id FROM src.session WHERE id IN (SELECT id FROM _adopt_sessions);",
    ]
    for table in present_tables:
        if table in _SESSION_SCOPED_MERGE_TABLES:
            key_column = _SESSION_KEY_BY_TABLE.get(table, "session_id")
            statements.append(
                f"INSERT OR IGNORE INTO main.{table} SELECT * FROM src.{table} "
                f"WHERE {key_column} IN (SELECT id FROM _adopt_sessions);"
            )
        else:
            key_column = _PROJECT_KEY_BY_TABLE.get(table, "project_id")
            statements.append(
                f"INSERT OR IGNORE INTO main.{table} SELECT * FROM src.{table} "
                f"WHERE {key_column} IN (SELECT id FROM _adopt_projects);"
            )
    statements.append("DROP TABLE _adopt_sessions;")
    statements.append("DROP TABLE _adopt_projects;")
    statements.append("DETACH DATABASE src;")
    return "".join(statements)


def apply_opencode_merge(dest_db: Path, staged_source_db: Path, session_id: str) -> None:
    """Merge ``session_id`` (and its descendants) from ``staged_source_db`` into a LOCAL ``dest_db``.

    Applied via the stdlib ``sqlite3`` module (not the host ``sqlite3`` CLI), so no CLI dependency on
    the destination host. Reads which tables the source actually has (:func:`list_source_merge_tables`)
    and runs :func:`build_opencode_merge_sql`, which attaches the staged source and copies the session's
    connected rows. Both dbs are local staging paths.
    """
    present_tables = list_source_merge_tables(staged_source_db)
    connection = sqlite3.connect(dest_db)
    try:
        connection.executescript(build_opencode_merge_sql(staged_source_db, session_id, present_tables))
        connection.commit()
    finally:
        connection.close()


def _sqlite_quote(value: str) -> str:
    """Quote a value as a SQLite string literal (single quotes doubled)."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"
