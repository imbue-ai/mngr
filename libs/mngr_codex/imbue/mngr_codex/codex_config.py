"""Read/write helpers for the OpenAI Codex CLI (``codex``) under a per-agent ``CODEX_HOME``.

The Codex CLI exposes a first-class config-dir override env var, ``CODEX_HOME``
(default ``~/.codex``). ``mngr_codex`` points each agent at its own ``CODEX_HOME``
under the agent state dir and leaves the user's real ``$HOME`` untouched. Codex
resolves its entire config/auth/session/hook tree from ``CODEX_HOME``:

    <CODEX_HOME>/
      config.toml        # model, approval, sandbox, trust, notices (mngr-owned)
      hooks.json         # the lifecycle-marker hooks (mngr-owned)
      auth.json          # credentials -- a symlink to the user's shared ~/.codex/auth.json
      .personality_migration  # NUX skip marker (mngr-owned, empty)
      sessions/YYYY/MM/DD/rollout-*-<uuid>.jsonl   # codex-owned transcripts

This module holds the pure, host-agnostic pieces of that scheme:

* Path builders (``get_codex_*_path``) that take a ``CODEX_HOME`` root, so the
  same functions address both the user's real ``~/.codex`` (the auth source) and
  each agent's isolated ``CODEX_HOME`` (the destination).
* ``build_codex_config`` -- layers the per-agent ``config.toml`` body from the
  agent's model/sandbox/approval knobs, the credential-store pin, the NUX-notice
  suppressors, the trusted work-dir, and a free-form ``config_overrides`` blob
  (applied last, so it wins). Pinning ``cli_auth_credentials_store = "file"`` is
  load-bearing: the keyring/auto backends hash ``CODEX_HOME`` into the secret
  key, which would make the shared-auth symlink unusable.
* ``build_codex_hooks_config`` -- the lifecycle ``active``-marker hooks written
  to ``<CODEX_HOME>/hooks.json``. Because codex subagents run *asynchronously*
  (the root's ``Stop`` fires while subagents are still running, with no
  ``fullyIdle`` signal and no ordering guarantee on the later ``SubagentStop``
  hooks), the marker is recomputed under a lock from a root-turn flag plus one
  file per in-flight subagent, so it stays present until the root turn **and**
  every subagent are done: ``UserPromptSubmit`` -> ``set_active_marker.sh`` (set
  the root-turn flag, record the root session id + transcript path), ``Stop`` ->
  ``clear_active_marker.sh`` (clear the root-turn flag for the recorded root
  session, then recompute), ``SubagentStart`` -> ``subagent_started.sh`` and
  ``SubagentStop`` -> ``subagent_stopped.sh`` (register/deregister the subagent).
* ``merge_project_trust`` -- the additive, idempotent ``[projects."<path>"]
  trust_level = "trusted"`` write used both to persist durable trust in the
  user's global ``config.toml`` and to seed the per-agent one.

Trust note: codex gates its first-launch "trust this folder?" dialog on the
project's ``trust_level`` being set. Seeding the work-dir as ``trusted`` skips
the dialog; it *also* enables codex to load any repo-local ``.codex/hooks.json``,
which -- combined with the ``--dangerously-bypass-hook-trust`` flag the plugin
passes so its own lifecycle hooks run -- is why trusting is consent-gated in the
plugin (see ``CodexAgent._ensure_source_repo_trusted``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import tomlkit

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import UserInputError
from imbue.mngr.interfaces.host import OnlineHostInterface

# ---------------------------------------------------------------------------
# CODEX_HOME layout
# ---------------------------------------------------------------------------

# Per-agent ``CODEX_HOME`` under the agent state dir. Codex resolves its whole
# config/auth/session/hook tree from here (set via the ``CODEX_HOME`` env var on
# the codex process).
CODEX_HOME_RELATIVE_PATH: tuple[str, ...] = ("plugin", "codex", "home")

# Codex's native resumable rollout store (the dir ``codex resume`` reads), as a
# POSIX rel-path under the agent state dir. Preserved on destroy; it is a sibling
# of the auth symlink and config, so targeting it specifically excludes those.
SESSIONS_RELATIVE_PATH: str = Path(*CODEX_HOME_RELATIVE_PATH, "sessions").as_posix()

_CONFIG_FILENAME: str = "config.toml"
_AUTH_FILENAME: str = "auth.json"
_HOOKS_FILENAME: str = "hooks.json"
# First-run NUX gate: codex skips the personality-migration prompt when this
# marker file exists (it auto-writes one on a fresh home with no sessions, but
# seeding it makes the silent-launch behavior explicit and order-independent).
_PERSONALITY_MIGRATION_FILENAME: str = ".personality_migration"
# codex maintains this update-cache file itself (see ``get_codex_version_cache_path``).
_VERSION_CACHE_FILENAME: str = "version.json"


def get_codex_home(agent_state_dir: Path) -> Path:
    """Return the per-agent ``CODEX_HOME`` directory under ``agent_state_dir``."""
    return agent_state_dir.joinpath(*CODEX_HOME_RELATIVE_PATH)


def get_codex_config_path(codex_home: Path) -> Path:
    """Return the ``config.toml`` path under ``codex_home``."""
    return codex_home / _CONFIG_FILENAME


def get_codex_auth_path(codex_home: Path) -> Path:
    """Return the ``auth.json`` path under ``codex_home``."""
    return codex_home / _AUTH_FILENAME


def get_codex_hooks_path(codex_home: Path) -> Path:
    """Return the ``hooks.json`` path under ``codex_home``."""
    return codex_home / _HOOKS_FILENAME


def get_codex_personality_migration_path(codex_home: Path) -> Path:
    """Return the ``.personality_migration`` NUX-skip marker path under ``codex_home``."""
    return codex_home / _PERSONALITY_MIGRATION_FILENAME


def get_codex_version_cache_path(codex_home: Path) -> Path:
    """Return codex's own ``version.json`` update-cache path under ``codex_home``.

    Codex maintains this file itself: ``{"latest_version": "0.139.0",
    "last_checked_at": "...", "dismissed_version": null}``. ``latest_version`` is the
    newest release codex last fetched -- it always records the true latest, even when
    up to date -- so mngr reads the *user's real* ``~/.codex/version.json`` to learn the
    latest version with no network call (codex refreshes it on its own throttled ~20h
    schedule during the user's direct codex use). Note a per-agent ``CODEX_HOME`` with
    ``check_for_update_on_startup = false`` never writes one, which is why we read the
    shared home, not the agent's.
    """
    return codex_home / _VERSION_CACHE_FILENAME


# ---------------------------------------------------------------------------
# Lifecycle-marker tracking files and hook script names
# ---------------------------------------------------------------------------

# Marker file (in ``$MNGR_AGENT_STATE_DIR``) whose presence ``BaseAgent``'s
# ``get_lifecycle_state`` reads as RUNNING; its absence means WAITING. Name kept
# in sync with the literal ``"active"`` that core checks and that the hook
# scripts touch/remove.
ACTIVE_MARKER_FILENAME: str = "active"

# Marker file (in ``$MNGR_AGENT_STATE_DIR``) present while codex is blocked on a
# tool-approval dialog. The ``PermissionRequest`` hook touches it; ``PostToolUse``
# (the tool ran after approval) and the root ``Stop`` (a stranded dialog at turn
# end) remove it. ``CodexAgent.get_lifecycle_state`` promotes RUNNING -> WAITING
# while it is present, and ``_waiting_reason`` reports ``PERMISSIONS``. Unlike the
# ``active`` marker it is a plain touch/remove flag, not part of the lock-guarded
# recompute: it tracks a single blocking dialog, not concurrent activity. This name
# is also hardcoded as a literal in ``codex_marker_state.sh``; keep the two in sync.
PERMISSIONS_WAITING_FILENAME: str = "permissions_waiting"

# Per-agent file (in ``$MNGR_AGENT_STATE_DIR``) recording the *root* codex
# session id for the current conversation -- the session that opened a turn while
# the marker was absent. ``clear_active_marker.sh`` acts on a ``Stop`` only when
# its ``session_id`` matches this, so a nested/recursive ``codex`` process
# sharing this ``CODEX_HOME`` cannot flip the root agent to WAITING.
# ``assemble_command`` also reads it to resume the conversation via
# ``codex resume <id>``. The shell scripts reference this same literal.
ROOT_SESSION_FILENAME: str = "codex_root_session"

# Per-agent flag file (in ``$MNGR_AGENT_STATE_DIR``) present while the *root*
# turn's model loop is running. ``set_active_marker.sh`` touches it on a fresh
# root turn; ``clear_active_marker.sh`` removes it on the root's ``Stop``. It is
# one of the two inputs to the marker invariant (the marker exists iff this flag
# is present or a subagent is in flight).
ROOT_ACTIVE_FILENAME: str = "codex_root_active"

# Per-agent directory (in ``$MNGR_AGENT_STATE_DIR``) holding one empty file per
# in-flight subagent, named by the subagent's ``agent_id``. ``subagent_started.sh``
# creates a file when a subagent starts; ``subagent_stopped.sh`` removes it when
# the subagent stops. A non-empty directory is the second input to the marker
# invariant -- it keeps the marker present (RUNNING) while async subagents run on
# after the root turn's ``Stop``.
SUBAGENTS_DIRNAME: str = "codex_subagents"

# Per-agent lock directory (in ``$MNGR_AGENT_STATE_DIR``) used as an mkdir-based
# mutex so the four hooks serialize their read-modify-recompute of the marker
# state. The shared helper acquires/releases it and steals it if a crashed hook
# left it stale.
MARKER_LOCK_DIRNAME: str = "codex_marker.lock"

# Per-agent file (in ``$MNGR_AGENT_STATE_DIR``) recording the absolute path of
# the root session's rollout JSONL (codex hands it to hooks as
# ``transcript_path``). ``stream_transcript.sh`` tails the file named here. The
# capture script hardcodes this same literal.
TRANSCRIPT_PATH_FILENAME: str = "codex_transcript_path"

# tmux wait-for channel prefix that the ``UserPromptSubmit`` hook signals once a
# turn is submitted (and *after* the ``active`` marker is set). The full channel is
# ``<prefix><tmux session>``. ``CodexAgent._send_enter_and_validate`` waits on this
# channel so ``send_message`` returns only after the agent reads RUNNING, closing
# the race between submitting a message and the lifecycle state flipping.
# ``set_active_marker.sh`` hardcodes this same literal (a test keeps them in sync).
SUBMIT_WAIT_CHANNEL_PREFIX: str = "mngr-submit-"

# Scripts provisioned into ``$MNGR_AGENT_STATE_DIR/commands/``; names kept in
# sync with the resource files under ``resources/``.
SET_ACTIVE_MARKER_SCRIPT_NAME: str = "set_active_marker.sh"
CLEAR_ACTIVE_MARKER_SCRIPT_NAME: str = "clear_active_marker.sh"
SUBAGENT_STARTED_SCRIPT_NAME: str = "subagent_started.sh"
SUBAGENT_STOPPED_SCRIPT_NAME: str = "subagent_stopped.sh"
# Shared POSIX-sh helper sourced by the four lifecycle hooks. Defines the marker
# state paths, the mkdir-based lock, and the recompute that enforces the invariant.
MARKER_STATE_LIB_SCRIPT_NAME: str = "codex_marker_state.sh"
BACKGROUND_TASKS_SCRIPT_NAME: str = "codex_background_tasks.sh"
RAW_TRANSCRIPT_SCRIPT_NAME: str = "stream_transcript.sh"
COMMON_TRANSCRIPT_SCRIPT_NAME: str = "common_transcript.sh"
# The python converter that common_transcript.sh invokes (python3
# <dir>/common_transcript_convert.py). Provisioned alongside the .sh so the shell
# resolves it relative to itself; gated by the same emit_common_transcript.
COMMON_TRANSCRIPT_CONVERT_SCRIPT_NAME: str = "common_transcript_convert.py"

# Output locations (under ``$MNGR_AGENT_STATE_DIR``) for the transcript layers:
# raw bytes under ``logs/`` and the agent-agnostic common transcript under
# ``events/``.
RAW_TRANSCRIPT_OUTPUT_RELATIVE: str = "logs/codex_transcript/events.jsonl"
COMMON_TRANSCRIPT_OUTPUT_RELATIVE: str = "events/codex/common_transcript/events.jsonl"


# ---------------------------------------------------------------------------
# config.toml
# ---------------------------------------------------------------------------

# Pinning the file credential store is load-bearing for shared auth: the
# ``keyring``/``auto``/``ephemeral`` backends key the secret by a hash of the
# canonical ``CODEX_HOME`` path, so each per-agent home would get a *different*
# entry and the shared-auth symlink would never be read. ``file`` keeps the
# secret in ``auth.json`` (which we symlink to the shared one). It is codex's
# current default, but ``auto`` exists (and prefers the OS keyring when present),
# so we pin it explicitly for cross-platform robustness.
_CREDENTIAL_STORE_KEY: str = "cli_auth_credentials_store"
_CREDENTIAL_STORE_FILE: str = "file"

# Disable codex's startup update check. On launch (including ``codex resume``)
# codex otherwise shows a BLOCKING "Update available! ... 1. Update now / 2. Skip
# / 3. Skip until next version" prompt that intercepts the composer -- which would
# misdirect mngr's first pasted message into the menu (and an Enter could even
# select "Update now", running ``brew upgrade``). Updates are the user's concern,
# not the agent's, so we always turn this off. (config-reference:
# "Check for Codex updates on startup (set to false only when updates are
# centrally managed)".)
_CHECK_FOR_UPDATE_KEY: str = "check_for_update_on_startup"

PROJECTS_KEY: str = "projects"
TRUST_LEVEL_KEY: str = "trust_level"
TRUST_LEVEL_TRUSTED: str = "trusted"

# First-run notice suppressors (codex's ``[notice]`` table). All booleans; an
# unknown-to-this-version key is inert, so seeding the full set is safe and keeps
# the first launch silent regardless of which migration prompts this codex build
# would otherwise show.
_NOTICE_SUPPRESSORS: Mapping[str, bool] = {
    "hide_full_access_warning": True,
    "hide_world_writable_warning": True,
    "hide_rate_limit_model_nudge": True,
}


def read_codex_config(host: OnlineHostInterface, config_path: Path) -> dict[str, Any]:
    """Read a codex ``config.toml`` via the host filesystem into a plain dict.

    A missing or empty file yields an empty dict so provisioning can fall
    through into a clean write. Malformed TOML raises ``UserInputError`` rather
    than being silently treated as empty: the user's real ``config.toml`` is
    state codex itself reads at every launch, and silently discarding it would
    let mngr overwrite content the user hand-edited. Aligns with the
    ``check_silent_decode_error_catches`` ratchet's user-config rule.
    """
    try:
        content = host.read_text_file(config_path)
    except FileNotFoundError:
        return {}
    if not content.strip():
        return {}
    try:
        parsed = tomlkit.parse(content)
    except Exception as exc:  # noqa: BLE001 -- tomlkit raises several parse-error subtypes; surface them all as user-facing.
        raise UserInputError(
            f"Codex config at {config_path} contains malformed TOML ({exc}); refusing to "
            f"overwrite. Inspect the file by hand and either fix it or remove it, then re-run."
        ) from exc
    return _tomlkit_to_plain_dict(parsed)


def _tomlkit_to_plain_dict(value: Any) -> dict[str, Any]:
    """Convert a parsed tomlkit document/table into plain Python dicts.

    ``json.loads(json.dumps(...))`` would lose nothing we need and drops the
    tomlkit proxy types, but tomlkit values are not always JSON-serializable
    (dates), so walk the mapping explicitly.
    """

    def convert(node: Any) -> Any:
        if isinstance(node, Mapping):
            return {str(k): convert(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [convert(v) for v in node]
        return node

    converted = convert(value)
    if not isinstance(converted, dict):
        return {}
    return converted


@pure
def build_codex_config(
    *,
    model: str | None,
    model_reasoning_effort: str | None,
    sandbox_mode: str | None,
    approval_policy: str | None,
    trusted_projects: Sequence[str],
    config_overrides: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a per-agent ``config.toml`` body (low -> high precedence).

    1. The fixed pins -- the credential store (``cli_auth_credentials_store =
       "file"``), the startup update check off (``check_for_update_on_startup =
       false``, so the blocking update prompt never intercepts the first
       message), and the ``[notice]`` suppressors -- always present so shared
       auth works and the first launch is silent.
    2. ``model`` / ``model_reasoning_effort`` / ``sandbox_mode`` /
       ``approval_policy`` -- each written only when not ``None`` (``None`` leaves
       codex's own default in force). ``model`` is intentionally not defaulted
       here: codex picks the account's default, and a ChatGPT-account login
       rejects some ``*-codex`` model slugs, so forcing one could break the
       agent (see the README's model note).
    3. ``trusted_projects`` -- each path written as ``[projects."<path>"]
       trust_level = "trusted"`` so codex's folder-trust dialog is skipped.
    4. ``config_overrides`` -- the per-agent-type blob, merged last (shallow) so
       it wins; covers anything not surfaced as a typed knob.

    Returns a plain dict; ``serialize_codex_config`` renders it as TOML.
    """
    config: dict[str, Any] = {
        _CREDENTIAL_STORE_KEY: _CREDENTIAL_STORE_FILE,
        # Always off: the blocking startup update prompt would intercept the
        # first message (see the constant's comment).
        _CHECK_FOR_UPDATE_KEY: False,
    }
    if model is not None:
        config["model"] = model
    if model_reasoning_effort is not None:
        config["model_reasoning_effort"] = model_reasoning_effort
    if sandbox_mode is not None:
        config["sandbox_mode"] = sandbox_mode
    if approval_policy is not None:
        config["approval_policy"] = approval_policy
    config["notice"] = dict(_NOTICE_SUPPRESSORS)

    projects: dict[str, Any] = {}
    for project_path in trusted_projects:
        projects[project_path] = {TRUST_LEVEL_KEY: TRUST_LEVEL_TRUSTED}
    if projects:
        config[PROJECTS_KEY] = projects

    # Shallow merge: a top-level override key replaces the built-in value
    # wholesale.
    for key, value in config_overrides.items():
        config[key] = value
    return config


@pure
def serialize_codex_config(config: Mapping[str, Any]) -> str:
    """Serialize a ``config.toml`` body as TOML via tomlkit.

    tomlkit quotes table keys correctly, which matters for the
    ``[projects."<abs-path>"]`` tables whose keys are filesystem paths. The file
    is mngr-owned, so exact formatting only affects diff readability.
    """
    document = tomlkit.document()
    for key, value in config.items():
        document[key] = value
    return tomlkit.dumps(document)


@pure
def merge_project_trust(config: Mapping[str, Any], project_path: str) -> dict[str, Any] | None:
    """Add ``[projects."<project_path>"] trust_level = "trusted"``; ``None`` if already trusted.

    Returns ``None`` when no change is required (the project is already present
    with ``trust_level = "trusted"``); otherwise a fresh dict with the project
    added/updated. Used both to persist durable trust in the user's global
    ``config.toml`` and -- via ``build_codex_config`` -- to seed the per-agent
    one. The path key is used verbatim: the caller passes the canonical absolute
    path codex matches against (codex canonicalizes the cwd, resolving symlinks,
    before lookup).

    A non-mapping ``projects`` value, or a non-mapping entry for this exact path,
    raises ``UserInputError`` rather than being silently overwritten -- the
    user's global config is hand-editable state.
    """
    existing_projects_raw = config.get(PROJECTS_KEY, {})
    if not isinstance(existing_projects_raw, Mapping):
        raise UserInputError(
            f"Codex config has a non-table `{PROJECTS_KEY}` value "
            f"({type(existing_projects_raw).__name__}); refusing to overwrite. Inspect the "
            f"file by hand and either fix the value or remove the key, then re-run."
        )
    existing_entry = existing_projects_raw.get(project_path)
    if existing_entry is not None and not isinstance(existing_entry, Mapping):
        raise UserInputError(
            f"Codex config has a non-table entry for project {project_path!r} "
            f"({type(existing_entry).__name__}); refusing to overwrite. Inspect the file by "
            f"hand and either fix the value or remove the key, then re-run."
        )
    if isinstance(existing_entry, Mapping) and existing_entry.get(TRUST_LEVEL_KEY) == TRUST_LEVEL_TRUSTED:
        return None

    merged: dict[str, Any] = dict(config)
    merged_projects: dict[str, Any] = {str(k): v for k, v in existing_projects_raw.items()}
    updated_entry: dict[str, Any] = dict(existing_entry) if isinstance(existing_entry, Mapping) else {}
    updated_entry[TRUST_LEVEL_KEY] = TRUST_LEVEL_TRUSTED
    merged_projects[project_path] = updated_entry
    merged[PROJECTS_KEY] = merged_projects
    return merged


def is_project_trusted(config: Mapping[str, Any], project_path: str) -> bool:
    """Return whether ``project_path`` is recorded as ``trusted`` in ``config``."""
    projects_raw = config.get(PROJECTS_KEY, {})
    if not isinstance(projects_raw, Mapping):
        return False
    entry = projects_raw.get(project_path)
    return isinstance(entry, Mapping) and entry.get(TRUST_LEVEL_KEY) == TRUST_LEVEL_TRUSTED


# ---------------------------------------------------------------------------
# Update check (codex's version.json)
# ---------------------------------------------------------------------------

# Key under which codex records the newest release it has seen, in version.json.
_VERSION_CACHE_LATEST_KEY: str = "latest_version"

# A clean numeric-dotted semver (e.g. ``0.138.0``). Anything with a pre-release or
# build suffix (``0.139.0-rc.1``) is deliberately treated as unparseable so we never
# raise a spurious update notice -- this mirrors codex's own conservative ``is_newer``,
# which returns "unknown" for non-numeric versions.
_CLEAN_SEMVER_RE = re.compile(r"\A\d+(?:\.\d+)*\Z")


def parse_codex_cli_version(version_output: str) -> str | None:
    """Extract the bare semver from ``codex --version`` output (``codex-cli 0.138.0`` -> ``0.138.0``).

    Returns the first whitespace-delimited token that is a clean numeric-dotted
    semver, or None if there is none (e.g. an empty result when codex is not
    installed, or a source/pre-release build). None just means the caller skips the
    update check rather than risk a false notice.
    """
    for token in version_output.split():
        if _CLEAN_SEMVER_RE.match(token):
            return token
    return None


def extract_latest_codex_version(version_cache: Mapping[str, Any]) -> str | None:
    """Return the ``latest_version`` from a parsed codex ``version.json``, or None.

    None if the key is missing or is not a clean semver string. The caller decodes
    the JSON (surfacing any decode error at warning level); this pure helper only
    pulls and validates the field.
    """
    latest = version_cache.get(_VERSION_CACHE_LATEST_KEY)
    if not isinstance(latest, str):
        return None
    return latest if _CLEAN_SEMVER_RE.match(latest) else None


def is_codex_update_available(installed_version: str, latest_version: str) -> bool:
    """Return True iff ``latest_version`` is strictly newer than ``installed_version``.

    Both are parsed as integer tuples (so ``0.10.0`` > ``0.9.0``); any unparseable
    input yields False -- no false-positive notice -- mirroring codex's conservative
    ``is_newer``.
    """
    installed = _parse_semver_tuple(installed_version)
    latest = _parse_semver_tuple(latest_version)
    if installed is None or latest is None:
        return False
    return latest > installed


def _parse_semver_tuple(version: str) -> tuple[int, ...] | None:
    """Parse a clean numeric-dotted semver into an int tuple, or None if not clean."""
    if not _CLEAN_SEMVER_RE.match(version):
        return None
    return tuple(int(part) for part in version.split("."))


# ---------------------------------------------------------------------------
# hooks.json
# ---------------------------------------------------------------------------

# Commands codex runs for each lifecycle event (``type: "command"`` handlers
# receive the event JSON on stdin). ``$MNGR_AGENT_STATE_DIR`` expands in codex's
# shell at hook-execution time. The scripts live in the agent's commands/ dir
# (provisioned by the plugin).
_SET_ACTIVE_COMMAND: str = f'bash "$MNGR_AGENT_STATE_DIR/commands/{SET_ACTIVE_MARKER_SCRIPT_NAME}"'
_CLEAR_ACTIVE_COMMAND: str = f'bash "$MNGR_AGENT_STATE_DIR/commands/{CLEAR_ACTIVE_MARKER_SCRIPT_NAME}"'
_SUBAGENT_STARTED_COMMAND: str = f'bash "$MNGR_AGENT_STATE_DIR/commands/{SUBAGENT_STARTED_SCRIPT_NAME}"'
_SUBAGENT_STOPPED_COMMAND: str = f'bash "$MNGR_AGENT_STATE_DIR/commands/{SUBAGENT_STOPPED_SCRIPT_NAME}"'

# The permission-waiting marker is a plain touch/remove flag (no shared lock /
# recompute), so its two hooks are inline one-liners rather than provisioned
# scripts. ``PermissionRequest`` fires (and blocks the agent) while an approval
# dialog is open; ``PostToolUse`` fires once the approved tool has run. (Codex has
# no ``PostToolUseFailure`` event -- verified against codex 0.139.0 -- so unlike
# claude there is no third clear hook; the root ``Stop`` clears any stranded
# marker as a safety net, in ``clear_active_marker.sh``.)
_SET_PERMISSIONS_WAITING_COMMAND: str = f'touch "$MNGR_AGENT_STATE_DIR/{PERMISSIONS_WAITING_FILENAME}"'
_CLEAR_PERMISSIONS_WAITING_COMMAND: str = f'rm -f "$MNGR_AGENT_STATE_DIR/{PERMISSIONS_WAITING_FILENAME}"'


@pure
def build_codex_hooks_config() -> dict[str, Any]:
    """Build the per-agent ``hooks.json`` body for the codex agent.

    Four handlers maintain the ``active`` lifecycle marker. Because codex
    subagents run *asynchronously* -- the root's ``Stop`` fires while subagents
    are still running, and their ``SubagentStop`` hooks arrive later with no
    ordering guarantee and no ``fullyIdle`` signal -- the marker is recomputed
    under a lock from two pieces of tracked state (a root-turn flag and one file
    per in-flight subagent) so it stays present until the root turn **and** every
    subagent are done. ``SubagentStart``/``SubagentStop`` are hooked so that the
    in-flight subagents can be tracked.

    * ``UserPromptSubmit`` -> ``set_active_marker.sh``: set the root-turn flag (so
      ``BaseAgent.get_lifecycle_state`` reports RUNNING) and, at a fresh root
      turn, record the root ``session_id`` and ``transcript_path`` and clear any
      stranded ``permissions_waiting`` marker (a second safety net alongside the
      root ``Stop``, so a new turn never inherits a prior dialog's state). After
      the marker is set it signals the ``mngr-submit-<session>`` tmux wait-for
      channel (``SUBMIT_WAIT_CHANNEL_PREFIX``), so ``send_message`` returns only
      once the agent reads RUNNING.
    * ``Stop`` -> ``clear_active_marker.sh``: clear the root-turn flag when the
      *root* agent's loop ends, then recompute (in-flight subagents keep the
      marker). The clear is guarded on the recorded root ``session_id`` so a
      nested/recursive ``codex`` process sharing this ``CODEX_HOME`` cannot flip
      the agent to WAITING. It also clears any stranded ``permissions_waiting``
      marker as a safety net.
    * ``SubagentStart`` -> ``subagent_started.sh``: register the subagent's
      ``agent_id`` so the marker stays present while it runs.
    * ``SubagentStop`` -> ``subagent_stopped.sh``: deregister the ``agent_id`` and
      recompute; the marker clears once the root turn is also done.

    Two further handlers maintain the ``permissions_waiting`` marker (a plain
    touch/remove flag, independent of the ``active`` recompute) so listings can
    report *why* a codex agent is waiting (verified live against codex 0.139.0):

    * ``PermissionRequest`` -> touch ``permissions_waiting``: codex fires this and
      blocks while a tool-approval dialog is open.
    * ``PostToolUse`` -> remove ``permissions_waiting``: the approved tool has run.

    The file is mngr-owned and rewritten from scratch each provision, so no
    merge-with-existing logic is needed. Codex requires command hooks to be
    trusted before they run; the plugin passes ``--dangerously-bypass-hook-trust``
    (consent-gated) rather than seeding a brittle, version-specific trust hash.
    """
    return {
        "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": _SET_ACTIVE_COMMAND}]}],
            "Stop": [{"hooks": [{"type": "command", "command": _CLEAR_ACTIVE_COMMAND}]}],
            "SubagentStart": [{"hooks": [{"type": "command", "command": _SUBAGENT_STARTED_COMMAND}]}],
            "SubagentStop": [{"hooks": [{"type": "command", "command": _SUBAGENT_STOPPED_COMMAND}]}],
            "PermissionRequest": [{"hooks": [{"type": "command", "command": _SET_PERMISSIONS_WAITING_COMMAND}]}],
            "PostToolUse": [{"hooks": [{"type": "command", "command": _CLEAR_PERMISSIONS_WAITING_COMMAND}]}],
        }
    }


@pure
def serialize_codex_hooks(hooks_config: Mapping[str, Any]) -> str:
    """Serialize a ``hooks.json`` body as two-space-indented JSON."""
    return json.dumps(dict(hooks_config), indent=2)


# ---------------------------------------------------------------------------
# Rollout cwd rebind (session adoption)
# ---------------------------------------------------------------------------

# A codex rollout JSONL records the session's working directory at ``payload.cwd``
# in two record types (verified against codex 0.138.0 rollouts): the single
# ``session_meta`` header record, and one ``turn_context`` record per turn. On
# launch, ``codex resume <id>`` compares the recorded cwd against the actual cwd; a
# mismatch pops the "Choose working directory to resume this session" modal. Adopting
# a session into a *new* work dir always mismatches, so adoption rewrites every
# recorded cwd to the new work dir. Other record types (``response_item``,
# ``event_msg``) carry no cwd; ``session_meta.payload.git`` records commit/branch
# only, no path.
_CWD_BEARING_ROLLOUT_RECORD_TYPES: frozenset[str] = frozenset({"session_meta", "turn_context"})
_ROLLOUT_PAYLOAD_KEY: str = "payload"
_ROLLOUT_CWD_KEY: str = "cwd"


@pure
def rewrite_rollout_record_cwd(record: Mapping[str, Any], new_cwd: str) -> dict[str, Any]:
    """Return ``record`` with its recorded ``payload.cwd`` rewritten to ``new_cwd``.

    Only the ``session_meta`` and ``turn_context`` records carry a ``payload.cwd``;
    every other record (and one whose payload lacks a ``cwd``) is returned unchanged
    as a plain dict. Operates on an already-parsed record so it stays pure: the JSONL
    decoding (and any malformed-line handling) is the caller's concern.
    """
    result = dict(record)
    if result.get("type") not in _CWD_BEARING_ROLLOUT_RECORD_TYPES:
        return result
    payload = result.get(_ROLLOUT_PAYLOAD_KEY)
    if isinstance(payload, Mapping) and _ROLLOUT_CWD_KEY in payload:
        result[_ROLLOUT_PAYLOAD_KEY] = {**payload, _ROLLOUT_CWD_KEY: new_cwd}
    return result
