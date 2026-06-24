import copy
import fcntl
import functools
import json
import os
import re
import shutil
from collections.abc import Generator
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import ConfigError
from imbue.mngr.utils.file_utils import atomic_write


class ClaudeDirectoryNotTrustedError(ConfigError):
    """The source directory is not trusted in Claude's config.

    When creating worktrees, we copy trust settings from the source directory
    to the worktree in ~/.claude.json. If the source directory itself is not
    trusted, the worktree won't be either, so Claude Code will show a trust
    dialog on startup. When mngr then uses tmux send-keys to deliver the
    initial prompt, the keystrokes will instead accept the trust dialog and
    be consumed, and the intended message will be lost. Worse, this silently
    grants trust to a directory the user never explicitly approved.
    """

    def __init__(self, source_path: str) -> None:
        self.source_path = source_path
        super().__init__(
            f"Source directory {source_path} is not trusted by Claude Code. "
            "Run `mngr create` interactively (without --no-connect) to be prompted, "
            f"or run Claude Code manually in {source_path} and accept the trust dialog."
        )


class ClaudeEffortCalloutNotDismissedError(ConfigError):
    """The effort callout has not been dismissed in Claude's global config."""

    def __init__(self) -> None:
        super().__init__(
            "Claude Code's effort callout has not been dismissed in ~/.claude.json. "
            "Run `mngr create` interactively (without --no-connect) to be prompted, "
            "or run Claude Code manually and dismiss the callout."
        )


class ClaudeOnboardingNotCompletedError(ConfigError):
    """Claude Code onboarding has not been completed."""

    def __init__(self) -> None:
        super().__init__(
            "Claude Code onboarding has not been completed in ~/.claude.json. "
            "Run `mngr create` interactively (without --no-connect) to be prompted, "
            "or run Claude Code manually to complete onboarding."
        )


class ClaudeBypassPermissionsNotAcceptedError(ConfigError):
    """The dangerous-mode safety warning has not been dismissed."""

    def __init__(self) -> None:
        super().__init__(
            "Claude Code's dangerous-mode safety warning has not been dismissed in ~/.claude.json. "
            "Run `mngr create` interactively (without --no-connect) to be prompted, "
            "or run Claude Code manually and dismiss the warning."
        )


def get_claude_config_dir() -> Path:
    """Return the Claude Code config directory.

    Reads $CLAUDE_CONFIG_DIR if set, otherwise defaults to ~/.claude/.
    This returns the "current" config directory -- inside an mngr agent it
    points to the per-agent isolated config dir.
    """
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".claude"


def get_user_claude_config_dir() -> Path:
    """Return the user-scope Claude Code config directory.

    Inside an mngr agent, $CLAUDE_CONFIG_DIR points to the agent's isolated
    config dir, but code that needs the user's original config (e.g. to copy
    credentials or settings) should call this function instead.

    Resolution order:
    1. $ORIGINAL_CLAUDE_CONFIG_DIR (set by mngr when creating agents), but
       only if that path actually exists as a directory on disk.
    2. Falls back to get_claude_config_dir() ($CLAUDE_CONFIG_DIR or ~/.claude/)

    The directory-existence check on $ORIGINAL_CLAUDE_CONFIG_DIR handles
    nested-sandbox scenarios (e.g. a Linux lima VM running on a macOS host):
    the env var is inherited from when the agent was first created on the
    host, so it points at a host path like /Users/<user>/.claude that does
    not exist inside the VM. Treating that as if the var were unset lets
    callers (most importantly the credentials provisioner) fall through to
    the per-agent CLAUDE_CONFIG_DIR, which is where the live credentials
    actually live in that scenario.
    """
    original = os.environ.get("ORIGINAL_CLAUDE_CONFIG_DIR")
    if original and Path(original).is_dir():
        return Path(original)
    return get_claude_config_dir()


def resolve_shared_claude_config_dir() -> Path:
    """Return $CLAUDE_CONFIG_DIR, falling back to ``~/.claude/`` when unset.

    Used by the shared (``isolate_local_config_dir=False``) mode of
    ``ClaudeAgentConfig`` where mngr delegates the claude config dir to whatever
    the user has in their shell env rather than provisioning a per-agent dir. The
    fallback to ``~/.claude/`` matches the directory claude itself picks when
    ``CLAUDE_CONFIG_DIR`` is unset. This resolves a *directory path* for mngr-side
    bookkeeping (e.g. locating shared session files); it is NOT what mngr exports
    into the agent's environment. Exporting ``CLAUDE_CONFIG_DIR=~/.claude`` is not
    equivalent to leaving it unset -- claude reads its global ``.claude.json`` from
    ``$CLAUDE_CONFIG_DIR/.claude.json`` when the var is set but from
    ``~/.claude.json`` when it is unset -- so ``modify_env_vars`` only propagates
    the var when the user's shell already had it set. The fallback path is shared
    (not per-agent), which is the whole point of the flag.
    """
    return get_claude_config_dir()


def find_user_config_in_unisolated_mode() -> Path:
    """Find the global ``.claude.json`` that claude reads in shared (non-isolated) mode.

    In shared mode (``isolate_local_config_dir=False``) mngr does not provision a
    per-agent config dir; the agent's claude reads the user's own global config.
    That file is ``$CLAUDE_CONFIG_DIR/.claude.json`` when the user's shell exports
    ``CLAUDE_CONFIG_DIR`` (the custom-dir convention) and ``~/.claude.json``
    otherwise (claude's default, beside ``~/.claude/``). This mirrors the directory
    resolution in ``resolve_shared_claude_config_dir`` and the ``CLAUDE_CONFIG_DIR``
    propagation in ``ClaudeAgent.modify_env_vars`` so that dialog-dismissal writes
    land in the same file the agent's claude will actually read.

    This differs from ``find_user_config_in_isolated_mode``, which keys off
    ``$ORIGINAL_CLAUDE_CONFIG_DIR`` (set only *inside* an agent) and is the right
    resolver for the isolated path; it would ignore a shared user's
    ``$CLAUDE_CONFIG_DIR`` and point at the wrong file.
    """
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        return Path(env_dir) / ".claude.json"
    return Path.home() / ".claude.json"


def find_user_config_in_isolated_mode() -> Path:
    """Find the user-scope Claude config file (.claude.json).

    Returns the first candidate path that exists on disk. If none exist,
    returns the first candidate as the default creation path.

    Inside an mngr agent, $CLAUDE_CONFIG_DIR points to the agent's isolated
    config dir. This function looks for the *user's* original config instead.

    Candidate paths when $ORIGINAL_CLAUDE_CONFIG_DIR is set:
    1. $ORIGINAL_CLAUDE_CONFIG_DIR/.claude.json (custom CLAUDE_CONFIG_DIR convention)
    2. Parent of $ORIGINAL_CLAUDE_CONFIG_DIR / .claude.json (default layout where the
       config file lives *beside* the config dir: ~/.claude/ dir -> ~/.claude.json file)

    Without $ORIGINAL_CLAUDE_CONFIG_DIR:
    1. ~/.claude.json (Claude Code's default location)
    """
    candidates: list[Path] = []

    original = os.environ.get("ORIGINAL_CLAUDE_CONFIG_DIR")
    if original:
        original_path = Path(original)
        candidates.append(original_path / ".claude.json")
        candidates.append(original_path.parent / ".claude.json")

    candidates.append(Path.home() / ".claude.json")

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


# =============================================================================
# Shared helpers for reading/writing claude config JSON
# =============================================================================


@contextmanager
def _claude_config_lock(config_path: Path) -> Generator[None, None, None]:
    """Acquire exclusive lock for the given config file and yield.

    Uses a separate .lock file (next to the config file) to avoid issues
    with atomic replacement of the config file itself.
    """
    lock_path = config_path.parent / (config_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)

    with open(lock_path, "r") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def read_claude_config(config_path: Path) -> dict[str, Any]:
    """Read and parse a claude config JSON file, returning empty dict if missing or empty."""
    if not config_path.exists():
        return {}
    content = config_path.read_text()
    if not content.strip():
        return {}
    return json.loads(content)


def _write_claude_config_atomic(config_path: Path, config: dict[str, Any]) -> None:
    """Atomically write config to the given path with backup.

    Creates a backup of the existing file (if any), then atomically writes
    the new content. Caller must hold the config lock.
    """
    if config_path.exists():
        backup_path = config_path.parent / (config_path.name + ".bak")
        shutil.copy2(config_path, backup_path)
        logger.trace("Created backup of Claude config at {}", backup_path)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(config_path, json.dumps(config, indent=2) + "\n")


# =============================================================================
# Trust operations
# =============================================================================


def is_source_directory_trusted(config_path: Path, source_path: Path) -> bool:
    """Check whether the source directory is trusted in the given config file.

    Returns True if source_path (or an ancestor) has hasTrustDialogAccepted=true
    in the config file at config_path.
    """
    source_path = source_path.resolve()

    config = read_claude_config(config_path)
    if not config:
        return False

    projects = config.get("projects", {})
    source_config = find_project_config(projects, source_path)
    if source_config is None:
        return False

    return bool(source_config.get("hasTrustDialogAccepted", False))


def check_source_directory_trusted(config_path: Path, source_path: Path) -> None:
    """Check that the source directory is trusted in the given config file.

    Reads the config file and verifies that source_path (or an ancestor) has
    hasTrustDialogAccepted=true.

    Raises ClaudeDirectoryNotTrustedError if the source is not trusted.
    """
    if not is_source_directory_trusted(config_path, source_path):
        raise ClaudeDirectoryNotTrustedError(str(source_path.resolve()))


def add_claude_trust_for_path(config_path: Path, source_path: Path) -> None:
    """Add trust for a directory in the given config file.

    Creates or updates the config file to mark the given path as trusted
    (hasTrustDialogAccepted=true). If the config file doesn't exist, it is
    created. If the path is already trusted, this is a no-op.
    """
    source_path = source_path.resolve()

    with _claude_config_lock(config_path):
        config = read_claude_config(config_path)
        projects = config.get("projects", {})
        source_path_str = str(source_path)

        # Check if already trusted
        existing = projects.get(source_path_str)
        if existing is not None and existing.get("hasTrustDialogAccepted", False):
            logger.trace("Claude trust already exists for {}", source_path)
            return

        # Add or update trust entry
        if existing is not None:
            projects[source_path_str] = {**existing, "hasTrustDialogAccepted": True}
        else:
            projects[source_path_str] = {"hasTrustDialogAccepted": True}

        config["projects"] = projects
        _write_claude_config_atomic(config_path, config)

    logger.trace("Added Claude trust for {}", source_path)


def remove_claude_trust_for_path(config_path: Path, path: Path) -> bool:
    """Remove Claude's trust entry for a path from the given config file.

    Removes the project entry for the given path from the config file.
    Used during agent cleanup to remove worktree trust entries.

    Returns True if the entry was removed, False if it didn't exist.
    Does not raise on errors - returns False and logs a warning instead.
    """
    path = path.resolve()

    with _claude_config_lock(config_path):
        try:
            config = read_claude_config(config_path)
        except json.JSONDecodeError as e:
            logger.warning("Failed to remove Claude trust entry for {}: {}", path, e)
            return False
        if not config:
            return False

        projects = config.get("projects", {})

        path_str = str(path)
        if path_str not in projects:
            logger.trace("Failed to find Claude trust entry for {}", path)
            return False

        # Only remove entries created by mngr to avoid removing user-created trust
        project_config = projects[path_str]
        if not project_config.get("_mngrCreated", False):
            logger.trace("Skipped removal of non-mngr trust entry for {}", path)
            return False

        del projects[path_str]
        config["projects"] = projects

        _write_claude_config_atomic(config_path, config)

    logger.trace("Removed Claude trust entry for {}", path)
    return True


def _get_boolean_flag(config_path: Path, key: str) -> bool:
    """Return whether the given boolean flag is set to true in the config file."""
    config = read_claude_config(config_path)
    return bool(config.get(key, False))


def _set_boolean_flag(config_path: Path, key: str) -> bool:
    """Set the given boolean flag to true in the config file. No-op if already set.

    Acquires the config lock, reads, and atomically writes the updated config.
    Returns True if it wrote (the flag was newly set), False if it was already set.
    """
    with _claude_config_lock(config_path):
        config = read_claude_config(config_path)
        if config.get(key, False):
            return False
        config[key] = True
        _write_claude_config_atomic(config_path, config)
    return True


def is_effort_callout_dismissed(config_path: Path) -> bool:
    """Check whether the effort callout has been dismissed in the given config file."""
    return _get_boolean_flag(config_path, "effortCalloutDismissed")


def check_effort_callout_dismissed(config_path: Path) -> None:
    """Check that the effort callout has been dismissed in the given config file.

    Raises ClaudeEffortCalloutNotDismissedError if the effort callout has not
    been dismissed.
    """
    if not is_effort_callout_dismissed(config_path):
        raise ClaudeEffortCalloutNotDismissedError()


def dismiss_effort_callout(config_path: Path) -> None:
    """Set effortCalloutDismissed=true in the given config file. No-op if already set."""
    if _set_boolean_flag(config_path, "effortCalloutDismissed"):
        logger.trace("Dismissed effort callout in Claude config")


def is_onboarding_completed(config_path: Path) -> bool:
    """Check whether onboarding has been completed in the given config file."""
    return _get_boolean_flag(config_path, "hasCompletedOnboarding")


def check_onboarding_completed(config_path: Path) -> None:
    """Check that onboarding has been completed. Raises ClaudeOnboardingNotCompletedError if not."""
    if not is_onboarding_completed(config_path):
        raise ClaudeOnboardingNotCompletedError()


def complete_onboarding(config_path: Path) -> None:
    """Set hasCompletedOnboarding=true in the given config file. No-op if already set."""
    if _set_boolean_flag(config_path, "hasCompletedOnboarding"):
        logger.trace("Marked onboarding as completed in Claude config")


def is_bypass_permissions_accepted(config_path: Path) -> bool:
    """Check whether the bypass permissions prompt has been accepted in the given config file."""
    return _get_boolean_flag(config_path, "bypassPermissionsModeAccepted")


def check_bypass_permissions_accepted(config_path: Path) -> None:
    """Check that bypass permissions has been accepted. Raises ClaudeBypassPermissionsNotAcceptedError if not."""
    if not is_bypass_permissions_accepted(config_path):
        raise ClaudeBypassPermissionsNotAcceptedError()


def accept_bypass_permissions(config_path: Path) -> None:
    """Set bypassPermissionsModeAccepted=true in the given config file. No-op if already set."""
    if _set_boolean_flag(config_path, "bypassPermissionsModeAccepted"):
        logger.trace("Accepted bypass permissions in Claude config")


def acknowledge_cost_threshold(config_path: Path) -> None:
    """Set hasAcknowledgedCostThreshold=true in the given config file. No-op if already set."""
    if _set_boolean_flag(config_path, "hasAcknowledgedCostThreshold"):
        logger.trace("Acknowledged cost threshold in Claude config")


def check_claude_dialogs_dismissed(config_path: Path, source_path: Path) -> None:
    """Check that all known Claude startup dialogs have been dismissed.

    Verifies that the config file is configured so that Claude Code can start
    without showing any dialogs that could intercept automated input.

    Raises the appropriate error for the first undismissed dialog found.
    """
    check_source_directory_trusted(config_path, source_path)
    check_effort_callout_dismissed(config_path)
    check_onboarding_completed(config_path)
    # Note: bypassPermissionsModeAccepted is NOT checked because Claude Code
    # periodically resets it to null. The bypass-permissions warning is handled
    # by skipDangerousModePermissionPrompt in settings.json instead.


def auto_dismiss_claude_dialogs(config_path: Path, source_path: Path) -> None:
    """Ensure all known Claude startup dialogs are marked as dismissed.

    Sets the necessary fields in the config file so that Claude Code can start
    without showing any dialogs. This is the remediation for errors raised by
    check_claude_dialogs_dismissed.
    """
    add_claude_trust_for_path(config_path, source_path)
    dismiss_effort_callout(config_path)
    complete_onboarding(config_path)
    acknowledge_cost_threshold(config_path)
    # bypassPermissionsModeAccepted: not set here (Claude Code resets it).
    # skipDangerousModePermissionPrompt in settings.json handles this instead.


def find_project_config(projects: Mapping[str, Any], path: Path) -> dict[str, Any] | None:
    """Find the project configuration for a path or its closest ancestor.

    Searches for an exact match first, then walks up the directory tree
    to find the closest ancestor with a configuration entry. Returns the
    project configuration dict if found, None otherwise.
    """
    path_str = str(path)
    if path_str in projects:
        return projects[path_str]

    current = path.parent
    root = Path(path.anchor)

    while current != root:
        current_str = str(current)
        if current_str in projects:
            return projects[current_str]
        current = current.parent

    # Check root as well
    if str(root) in projects:
        return projects[str(root)]

    return None


# =============================================================================
# Project Directory Encoding
# =============================================================================

# Matches every character that Claude Code's project-dir encoder maps to '-'
# (i.e. everything that is not an ASCII alphanumeric or literal '-').
_NON_DASH_ALNUM_ASCII: Final = re.compile(r"[^A-Za-z0-9-]")


@pure
def encode_claude_project_dir_name(path: Path) -> str:
    """Encode a filesystem path into Claude Code's project directory name.

    Claude Code stores per-project data in ~/.claude/projects/<encoded-path>/.
    The encoding keeps only ASCII alphanumerics and ``-``, mapping every
    other character (``/``, ``.``, ``_``, space, ``@``, ``+``, accented
    letters, CJK, etc.) to ``-`` -- per the algorithm documented in
    anthropics/claude-code#19972. If this encoder diverges from Claude
    Code's, ``on_after_provisioning`` writes the adopted JSONL to a
    project subdir Claude Code never reads on resume, the find guard in
    ``assemble_command`` returns no match, and ``--adopt``
    silently spawns a fresh session via the ``||`` fallback.
    """
    return _NON_DASH_ALNUM_ASCII.sub("-", str(path))


# =============================================================================
# Per-agent Claude artifact directory ($MNGR_AGENT_STATE_DIR/plugin/claude/)
# =============================================================================

# Single source of truth for the per-agent ``plugin/claude/`` layout (relative to
# $MNGR_AGENT_STATE_DIR). It holds the isolated config dir (``anthropic/``), the
# response-stream buffers, and the managed settings file, and is preserved as part
# of the agent's ``plugin/`` subtree on clone. Routing every accessor through
# ``get_agent_claude_plugin_dir`` keeps those call sites from drifting.
_AGENT_CLAUDE_PLUGIN_SUBPATH: Final[tuple[str, ...]] = ("plugin", "claude")

# Filename of the file holding all of mngr's Claude hooks, loaded via
# ``claude --settings``. Now used only in ``use_env_config_dir`` mode: there is no
# per-agent config dir to bake hooks into, so mngr loads them from this file
# instead. (In normal mode the hooks live in the per-agent config-dir
# ``settings.json`` -- the "user" layer Claude reads -- and this file is unused.)
# It lives in the agent's private state dir rather than the project's
# ``.claude/settings.local.json``, which every claude session in that directory
# reads (including plain non-mngr ones) -- so mngr's hooks take effect only inside
# the agent and never run in a plain ``claude`` session. mngr owns the file
# outright and rewrites it fresh each provision.
MANAGED_SETTINGS_FILENAME: Final[str] = "mngr_managed_settings.json"
MANAGED_SETTINGS_RELATIVE_PATH: Final[tuple[str, ...]] = (*_AGENT_CLAUDE_PLUGIN_SUBPATH, MANAGED_SETTINGS_FILENAME)


def get_agent_claude_plugin_dir(agent_state_dir: Path) -> Path:
    """Return the per-agent directory holding mngr's Claude artifacts.

    ``agent_state_dir`` is the agent's state directory (on-disk $MNGR_AGENT_STATE_DIR).
    The directory holds the per-agent config dir (``anthropic/``), the response-stream
    buffers, and the managed settings file. See ``_AGENT_CLAUDE_PLUGIN_SUBPATH``.
    """
    return agent_state_dir.joinpath(*_AGENT_CLAUDE_PLUGIN_SUBPATH)


# Subdirectory of the per-agent ``plugin/claude/`` dir that is the agent's isolated
# Claude config dir (the per-agent replacement for ``~/.claude/``). It holds the
# config-dir ``settings.json`` (the "user" layer Claude reads from
# $CLAUDE_CONFIG_DIR) and the session ``projects/`` subtree.
_AGENT_CLAUDE_CONFIG_SUBDIR: Final[str] = "anthropic"


def get_agent_claude_config_dir(agent_state_dir: Path) -> Path:
    """Return the agent's isolated Claude config dir (per-agent replacement for ~/.claude/).

    This is the directory Claude reads as ``$CLAUDE_CONFIG_DIR`` in normal mode;
    its ``settings.json`` is the "user" settings layer mngr builds. Single source
    of truth shared by ``ClaudeAgent.get_claude_config_dir`` and the
    subagent-proxy plugin so the path never drifts between them.
    """
    return get_agent_claude_plugin_dir(agent_state_dir) / _AGENT_CLAUDE_CONFIG_SUBDIR


def get_managed_settings_path(agent_state_dir: Path) -> Path:
    """Return the agent's mngr-managed Claude settings file. See ``MANAGED_SETTINGS_FILENAME``."""
    return get_agent_claude_plugin_dir(agent_state_dir) / MANAGED_SETTINGS_FILENAME


def get_agent_hook_settings_path(agent_state_dir: Path, *, use_env_config_dir: bool) -> Path:
    """Return the settings file mngr's Claude hooks live in for this agent.

    In ``use_env_config_dir`` mode the managed ``--settings`` file (no per-agent config dir
    exists); otherwise the per-agent config-dir ``settings.json`` (the "user" layer Claude
    reads, built by ``_build_settings_json``). Single source of truth shared by mngr_claude
    and the subagent-proxy plugin so the branch never drifts between them.
    """
    if use_env_config_dir:
        return get_managed_settings_path(agent_state_dir)
    return get_agent_claude_config_dir(agent_state_dir) / "settings.json"


# =============================================================================
# Readiness Hooks Configuration
# =============================================================================

# Guard prefix for readiness hook commands: exit gracefully if this is not the
# main Claude session (e.g. a reviewer sub-agent that resumed a session).
SESSION_GUARD: Final[str] = '[ -z "$MAIN_CLAUDE_SESSION_ID" ] && exit 0; '

# Shell snippet that marks the agent idle: removes the 'active' and
# 'permissions_waiting' marker files (so get_lifecycle_state reports WAITING
# rather than RUNNING) and emits an activity event so `mngr observe` promptly
# re-fetches the agent's state. Shared by the Notification idle_prompt hook and
# the SessionStart startup/resume hook so the two stay byte-identical.
_CLEAR_ACTIVE_MARKERS_AND_EMIT_ACTIVITY_EVENT: Final[str] = (
    """rm -f "$MNGR_AGENT_STATE_DIR/active" "$MNGR_AGENT_STATE_DIR/permissions_waiting" && mkdir -p $MNGR_HOST_DIR/events/mngr/activity && echo '{"source": "mngr/activity", "type": "activity", "event_id": "'"evt-$(head -c 16 /dev/urandom | xxd -p)"'", "timestamp": "'"$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z")"'"}' >> $MNGR_HOST_DIR/events/mngr/activity/events.jsonl"""
)


@pure
def build_readiness_hooks_config() -> dict[str, Any]:
    """Build the hooks configuration for readiness signaling and session tracking.

    These hooks use the MNGR_AGENT_STATE_DIR environment variable to create/remove
    files that signal agent state.

    - SessionStart: creates 'session_started' file AND tracks the current session ID
      (writes to claude_session_id and appends to claude_session_id_history). Also
      signals the tmux wait-for channel that ``mngr message`` waits on, but ONLY
      when the session start was triggered by ``/clear`` or ``/compact``. These
      are TUI-local slash commands that do NOT trigger UserPromptSubmit, so
      without this signal ``mngr message agent -m /clear`` would time out at
      ``enter_submission_timeout_seconds`` even though /clear actually executed.
      Filtering on source ensures normal startup/resume don't fire stale signals.
      Finally, on ``startup``/``resume`` it clears the 'active' and
      'permissions_waiting' markers (see below): a fresh Claude process is not
      mid-turn, so any marker left over from a turn that was abandoned by an
      abnormal exit (container restart, OOM, crash -- where the Stop hook never
      ran) is stale and must be reset, otherwise the agent reports RUNNING
      forever. ``compact`` is excluded because auto-compaction fires mid-turn
      while Claude is genuinely active.
    - UserPromptSubmit: creates 'active' file, removes 'permissions_waiting', signals tmux wait-for
    - PermissionRequest: creates 'permissions_waiting' file (Claude is waiting for permission approval)
    - PostToolUse: removes 'permissions_waiting' file (tool completed, permission resolved)
    - PostToolUseFailure: removes 'permissions_waiting' file (tool failed/denied, permission resolved)
    - Notification (idle_prompt): removes 'active' and 'permissions_waiting' files
    - Stop: runs wait_for_stop_hook.sh which waits for all other stop hooks to
      finish, then runs post-completion actions (uploads the current commit's
      autofix issue file to the Modal code-review-json volume when the
      code-guardian orchestrator wrote .reviewer/outputs/orchestrator_success,
      and invokes notify_user best-effort), and finally removes 'active' and
      'permissions_waiting' and emits an activity event

    File semantics:
    - session_started: Claude Code session has started (for initial message timing)
    - claude_session_id: current session UUID (atomically written via .tmp + mv)
    - claude_session_id_history: append-only log of session entries (one per line,
      format: "session_id source" where source comes from the hook payload)
    - active: Claude is processing user input (RUNNING lifecycle state, WAITING otherwise)
    - permissions_waiting: Claude is blocked on a permission dialog (always WAITING when present)
    - claude_process_started: touched on every startup/resume SessionStart (a
      fresh, not-mid-turn Claude process). Its mtime is the restart boundary:
      a transcript event older than it belongs to a turn the current process
      did not run, so consumers can treat such a tail as idle rather than
      "still working". Deliberately NOT touched on compact (mid-turn).

    The tmux wait-for signal on UserPromptSubmit allows instant detection of
    message submission without polling.
    """
    return {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": SESSION_GUARD + 'touch "$MNGR_AGENT_STATE_DIR/session_started"',
                        },
                        {
                            "type": "command",
                            "command": SESSION_GUARD
                            + 'echo "The base branch for this work is: ${MNGR_GIT_BASE_BRANCH:-main}"',
                        },
                        {
                            "type": "command",
                            "command": (
                                SESSION_GUARD + "_MNGR_HOOK_INPUT=$(cat);"
                                ' _MNGR_NEW_SID=$(echo "$_MNGR_HOOK_INPUT" | jq -r ".session_id // empty");'
                                ' if [ -z "$_MNGR_NEW_SID" ]; then'
                                ' echo "mngr: SessionStart hook failed to extract session_id from hook input: $_MNGR_HOOK_INPUT" >&2;'
                                " exit 1;"
                                " fi;"
                                ' _MNGR_SOURCE=$(echo "$_MNGR_HOOK_INPUT" | jq -r ".source // empty");'
                                ' echo "$_MNGR_NEW_SID" > "$MNGR_AGENT_STATE_DIR/claude_session_id.tmp"'
                                ' && mv "$MNGR_AGENT_STATE_DIR/claude_session_id.tmp" "$MNGR_AGENT_STATE_DIR/claude_session_id";'
                                ' echo "$_MNGR_NEW_SID${_MNGR_SOURCE:+ $_MNGR_SOURCE}" >> "$MNGR_AGENT_STATE_DIR/claude_session_id_history"'
                            ),
                        },
                        {
                            # /clear and /compact do not trigger UserPromptSubmit
                            # (they are TUI-local commands), so without this hook
                            # `mngr message agent -m /clear` would time out at
                            # `enter_submission_timeout_seconds` even though /clear
                            # ran successfully. Mirror the UserPromptSubmit signal
                            # here, gated on source so that normal startup/resume
                            # don't fire stale signals.
                            "type": "command",
                            "command": (
                                SESSION_GUARD + "_MNGR_HOOK_INPUT=$(cat);"
                                ' _MNGR_SOURCE=$(echo "$_MNGR_HOOK_INPUT" | jq -r ".source // empty");'
                                ' case "$_MNGR_SOURCE" in clear|compact)'
                                " tmux wait-for -S \"mngr-submit-$(tmux display-message -p '#S')\" 2>/dev/null || true ;;"
                                " esac"
                            ),
                        },
                        {
                            # A fresh Claude process (startup/resume) is never
                            # mid-turn, so record the process-start time and reset
                            # the activity markers. This heals the case where a
                            # turn was abandoned by an abnormal exit (container
                            # restart, OOM, crash): the Stop hook never ran to
                            # clear 'active', so without this the agent would
                            # report RUNNING forever after restart. The
                            # 'claude_process_started' marker's mtime gives
                            # consumers (e.g. the system interface activity
                            # indicator) a restart boundary to compare transcript
                            # timestamps against -- any transcript event older
                            # than it belongs to a turn this process did not run.
                            # Gated to startup|resume; compact is excluded because
                            # auto-compaction fires mid-turn while Claude is active
                            # (so it must NOT move the process-start boundary).
                            "type": "command",
                            "command": (
                                SESSION_GUARD + "_MNGR_HOOK_INPUT=$(cat);"
                                ' _MNGR_SOURCE=$(echo "$_MNGR_HOOK_INPUT" | jq -r ".source // empty");'
                                ' case "$_MNGR_SOURCE" in startup|resume)'
                                ' touch "$MNGR_AGENT_STATE_DIR/claude_process_started" && '
                                + _CLEAR_ACTIVE_MARKERS_AND_EMIT_ACTIVITY_EVENT
                                + " ;; esac"
                            ),
                        },
                    ]
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": SESSION_GUARD
                            + """touch "$MNGR_AGENT_STATE_DIR/active" && rm -f "$MNGR_AGENT_STATE_DIR/permissions_waiting" && mkdir -p $MNGR_HOST_DIR/events/mngr/activity && echo '{"source": "mngr/activity", "type": "activity", "event_id": "'"evt-$(head -c 16 /dev/urandom | xxd -p)"'", "timestamp": "'"$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z")"'"}' >> $MNGR_HOST_DIR/events/mngr/activity/events.jsonl""",
                        },
                        {
                            "type": "command",
                            "command": SESSION_GUARD
                            + "tmux wait-for -S \"mngr-submit-$(tmux display-message -p '#S')\" 2>/dev/null || true",
                        },
                    ]
                }
            ],
            "PermissionRequest": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": SESSION_GUARD + 'touch "$MNGR_AGENT_STATE_DIR/permissions_waiting"',
                        },
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": SESSION_GUARD + 'rm -f "$MNGR_AGENT_STATE_DIR/permissions_waiting"',
                        },
                    ],
                }
            ],
            "PostToolUseFailure": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": SESSION_GUARD + 'rm -f "$MNGR_AGENT_STATE_DIR/permissions_waiting"',
                        },
                    ],
                }
            ],
            "Notification": [
                {
                    "matcher": "idle_prompt",
                    "hooks": [
                        {
                            "type": "command",
                            "command": SESSION_GUARD + _CLEAR_ACTIVE_MARKERS_AND_EMIT_ACTIVITY_EVENT,
                        },
                    ],
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": SESSION_GUARD + 'bash "$MNGR_AGENT_STATE_DIR/commands/wait_for_stop_hook.sh"',
                        },
                    ],
                }
            ],
        }
    }


@pure
def build_permission_auto_allow_hooks_config() -> dict[str, Any]:
    """Build hooks configuration that auto-allows all permission dialogs.

    Adds a PermissionRequest hook with a wildcard matcher that outputs a JSON
    decision to allow every tool use without pausing for user approval.
    """
    return {
        "hooks": {
            "PermissionRequest": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "echo "
                                '\'{"hookSpecificOutput":{"hookEventName":"PermissionRequest",'
                                '"decision":{"behavior":"allow"}}}\''
                            ),
                            "timeout": 5,
                        }
                    ],
                }
            ],
        }
    }


@pure
def build_credential_sync_hooks_config() -> dict[str, Any]:
    """Build the hooks configuration for credential sync on macOS.

    Installs a Notification:auth_success hook that propagates keychain
    credentials from the current agent to all other per-agent keychain entries
    after a successful login.
    """
    return {
        "hooks": {
            "Notification": [
                {
                    "matcher": "auth_success",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'python3 "$MNGR_AGENT_STATE_DIR/commands/sync_keychain_credentials.py"',
                        },
                    ],
                }
            ],
        }
    }


@pure
def hook_already_exists(existing_hooks: list[dict[str, Any]], new_hook: dict[str, Any]) -> bool:
    """Check if a hook with the same command already exists in the list.

    Compares the inner hooks' commands to detect duplicates.
    """
    new_commands = {h.get("command") for h in new_hook.get("hooks", [])}
    for existing in existing_hooks:
        existing_commands = {h.get("command") for h in existing.get("hooks", [])}
        if new_commands == existing_commands:
            return True
    return False


def merge_hooks_config(existing_settings: dict[str, Any], hooks_config: dict[str, Any]) -> dict[str, Any]:
    """Merge new hooks into existing settings, skipping duplicates.

    Returns a new settings dict with any not-yet-present hooks appended -- equal by value
    to ``existing_settings`` when every hook already existed. Does not mutate the input; a
    caller that must avoid a redundant write compares the result against its input.
    """
    merged = copy.deepcopy(existing_settings)
    if "hooks" not in merged:
        merged["hooks"] = {}

    for event_name, event_hooks in hooks_config["hooks"].items():
        if event_name not in merged["hooks"]:
            merged["hooks"][event_name] = []

        for new_hook in event_hooks:
            if not hook_already_exists(merged["hooks"][event_name], new_hook):
                merged["hooks"][event_name].append(new_hook)

    return merged


def fold_hook_configs(base: dict[str, Any], hook_configs: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold a sequence of hook configs onto a base settings dict via ``merge_hooks_config``.

    Each config is merged in order, skipping duplicate hooks. Returns the resulting
    settings dict (equal by value to ``base`` if every hook already existed).
    """
    return functools.reduce(merge_hooks_config, hook_configs, base)
