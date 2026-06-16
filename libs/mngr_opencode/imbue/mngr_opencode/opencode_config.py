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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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


@pure
def build_opencode_config(
    base_config: Mapping[str, Any],
    config_overrides: Mapping[str, Any],
    is_auto_allow_permissions: bool,
) -> dict[str, Any]:
    """Build a per-agent ``opencode.json`` body by layering (low -> high precedence).

    1. ``base_config`` -- a copy of the user's real ``opencode.json`` (when
       ``sync_global_config``) or an empty dict. Copied, never mutated. Carries
       the user's model/provider/theme defaults so the agent starts usable.
    2. A wildcard allow ``permission`` block when ``is_auto_allow_permissions`` --
       auto-approves every action not explicitly denied (the config analog of
       ``--dangerously-skip-permissions``; a finer policy instead comes via
       ``config_overrides``).
    3. ``config_overrides`` -- the per-agent-type blob (``model``, ``permission``,
       ...), applied last so it wins.

    A ``$schema`` is always set so the file validates and editors autocomplete.
    """
    config: dict[str, Any] = dict(base_config)
    config[_SCHEMA_KEY] = _SCHEMA_URL
    if is_auto_allow_permissions:
        config[_PERMISSION_KEY] = {_PERMISSION_WILDCARD: _PERMISSION_ALLOW}
    config.update(config_overrides)
    return config


@pure
def serialize_opencode_config(config: Mapping[str, Any]) -> str:
    """Serialize an ``opencode.json`` body as two-space-indented JSON."""
    return json.dumps(dict(config), indent=2)
