import copy
import fcntl
import json
import shutil
from collections.abc import Generator
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mng.errors import ConfigError
from imbue.mng.utils.file_utils import atomic_write


class ClaudeDirectoryNotTrustedError(ConfigError):
    """The source directory is not trusted in Claude's config.

    When creating worktrees, we copy trust settings from the source directory
    to the worktree in ~/.claude.json. If the source directory itself is not
    trusted, the worktree won't be either, so Claude Code will show a trust
    dialog on startup. When mng then uses tmux send-keys to deliver the
    initial prompt, the keystrokes will instead accept the trust dialog and
    be consumed, and the intended message will be lost. Worse, this silently
    grants trust to a directory the user never explicitly approved.
    """

    def __init__(self, source_path: str) -> None:
        self.source_path = source_path
        super().__init__(
            f"Source directory {source_path} is not trusted by Claude Code. "
            "Run `mng create` interactively (without --no-connect) to be prompted, "
            f"or run Claude Code manually in {source_path} and accept the trust dialog."
        )


class ClaudeEffortCalloutNotDismissedError(ConfigError):
    """The effort callout has not been dismissed in Claude's global config."""

    def __init__(self) -> None:
        super().__init__(
            "Claude Code's effort callout has not been dismissed in ~/.claude.json. "
            "Run `mng create` interactively (without --no-connect) to be prompted, "
            "or run Claude Code manually and dismiss the callout."
        )


class ClaudeOnboardingNotCompletedError(ConfigError):
    """Claude Code onboarding has not been completed."""

    def __init__(self) -> None:
        super().__init__(
            "Claude Code onboarding has not been completed in ~/.claude.json. "
            "Run `mng create` interactively (without --no-connect) to be prompted, "
            "or run Claude Code manually to complete onboarding."
        )


class ClaudeBypassPermissionsNotAcceptedError(ConfigError):
    """The dangerous-mode safety warning has not been dismissed."""

    def __init__(self) -> None:
        super().__init__(
            "Claude Code's dangerous-mode safety warning has not been dismissed in ~/.claude.json. "
            "Run `mng create` interactively (without --no-connect) to be prompted, "
            "or run Claude Code manually and dismiss the warning."
        )


def get_claude_config_path() -> Path:
    """Return the path to the global Claude config file (~/.claude.json)."""
    return Path.home() / ".claude.json"


def get_claude_config_backup_path() -> Path:
    """Return the path to the global Claude config backup file."""
    return Path.home() / ".claude.json.bak"


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

        # Only remove entries created by mng to avoid removing user-created trust
        project_config = projects[path_str]
        if not project_config.get("_mngCreated", False):
            logger.trace("Skipped removal of non-mng trust entry for {}", path)
            return False

        del projects[path_str]
        config["projects"] = projects

        _write_claude_config_atomic(config_path, config)

    logger.trace("Removed Claude trust entry for {}", path)
    return True


def is_effort_callout_dismissed(config_path: Path) -> bool:
    """Check whether the effort callout has been dismissed in the given config file.

    Returns True if effortCalloutDismissed is true in the config file.
    """
    config = read_claude_config(config_path)
    return bool(config.get("effortCalloutDismissed", False))


def check_effort_callout_dismissed(config_path: Path) -> None:
    """Check that the effort callout has been dismissed in the given config file.

    Reads the config file and verifies that effortCalloutDismissed is true.

    Raises ClaudeEffortCalloutNotDismissedError if the effort callout has not
    been dismissed.
    """
    if not is_effort_callout_dismissed(config_path):
        raise ClaudeEffortCalloutNotDismissedError()


def dismiss_effort_callout(config_path: Path) -> None:
    """Set effortCalloutDismissed=true in the given config file.

    Acquires the config lock and sets the field. No-op if already set.
    """
    with _claude_config_lock(config_path):
        config = read_claude_config(config_path)
        if config.get("effortCalloutDismissed", False):
            return
        config["effortCalloutDismissed"] = True
        _write_claude_config_atomic(config_path, config)

    logger.trace("Dismissed effort callout in Claude config")


def is_onboarding_completed(config_path: Path) -> bool:
    """Check whether onboarding has been completed in the given config file."""
    config = read_claude_config(config_path)
    return bool(config.get("hasCompletedOnboarding", False))


def check_onboarding_completed(config_path: Path) -> None:
    """Check that onboarding has been completed. Raises ClaudeOnboardingNotCompletedError if not."""
    if not is_onboarding_completed(config_path):
        raise ClaudeOnboardingNotCompletedError()


def complete_onboarding(config_path: Path) -> None:
    """Set hasCompletedOnboarding=true in the given config file. No-op if already set."""
    with _claude_config_lock(config_path):
        config = read_claude_config(config_path)
        if config.get("hasCompletedOnboarding", False):
            return
        config["hasCompletedOnboarding"] = True
        _write_claude_config_atomic(config_path, config)

    logger.trace("Marked onboarding as completed in Claude config")


def is_bypass_permissions_accepted(config_path: Path) -> bool:
    """Check whether the bypass permissions prompt has been accepted in the given config file."""
    config = read_claude_config(config_path)
    return bool(config.get("bypassPermissionsModeAccepted", False))


def check_bypass_permissions_accepted(config_path: Path) -> None:
    """Check that bypass permissions has been accepted. Raises ClaudeBypassPermissionsNotAcceptedError if not."""
    if not is_bypass_permissions_accepted(config_path):
        raise ClaudeBypassPermissionsNotAcceptedError()


def accept_bypass_permissions(config_path: Path) -> None:
    """Set bypassPermissionsModeAccepted=true in the given config file. No-op if already set."""
    with _claude_config_lock(config_path):
        config = read_claude_config(config_path)
        if config.get("bypassPermissionsModeAccepted", False):
            return
        config["bypassPermissionsModeAccepted"] = True
        _write_claude_config_atomic(config_path, config)

    logger.trace("Accepted bypass permissions in Claude config")


def acknowledge_cost_threshold(config_path: Path) -> None:
    """Set hasAcknowledgedCostThreshold=true in the given config file. No-op if already set."""
    with _claude_config_lock(config_path):
        config = read_claude_config(config_path)
        if config.get("hasAcknowledgedCostThreshold", False):
            return
        config["hasAcknowledgedCostThreshold"] = True
        _write_claude_config_atomic(config_path, config)

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


def ensure_claude_dialogs_dismissed(config_path: Path, source_path: Path) -> None:
    """Ensure all known Claude startup dialogs are marked as dismissed.

    Sets the necessary fields in the config file so that Claude Code can start
    without showing any dialogs. This is the remediation for errors raised by
    check_claude_dialogs_dismissed.
    """
    add_claude_trust_for_path(config_path, source_path)
    dismiss_effort_callout(config_path)
    complete_onboarding(config_path)
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


@pure
def encode_claude_project_dir_name(path: Path) -> str:
    """Encode a filesystem path into Claude Code's project directory name.

    Claude Code stores per-project data in ~/.claude/projects/<encoded-path>/.
    The encoding replaces '/' and '.' with '-'.
    """
    return str(path).replace("/", "-").replace(".", "-")


# =============================================================================
# Readiness Hooks Configuration
# =============================================================================


@pure
def build_readiness_hooks_config() -> dict[str, Any]:
    """Build the hooks configuration for readiness signaling and session tracking.

    These hooks use the MNG_AGENT_STATE_DIR environment variable to create/remove
    files that signal agent state.

    - SessionStart: creates 'session_started' file AND tracks the current session ID
      (writes to claude_session_id and appends to claude_session_id_history)
    - UserPromptSubmit: creates 'active' file, removes 'permissions_waiting', signals tmux wait-for
    - PermissionRequest: creates 'permissions_waiting' file (Claude is waiting for permission approval)
    - PostToolUse: removes 'permissions_waiting' file (tool completed, permission resolved)
    - PostToolUseFailure: removes 'permissions_waiting' file (tool failed/denied, permission resolved)
    - Notification (idle_prompt): removes 'active' and 'permissions_waiting' files

    File semantics:
    - session_started: Claude Code session has started (for initial message timing)
    - claude_session_id: current session UUID (atomically written via .tmp + mv)
    - claude_session_id_history: append-only log of session entries (one per line,
      format: "session_id source" where source comes from the hook payload)
    - active: Claude is processing user input (RUNNING lifecycle state, WAITING otherwise)
    - permissions_waiting: Claude is blocked on a permission dialog (always WAITING when present)

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
                            "command": 'touch "$MNG_AGENT_STATE_DIR/session_started"',
                        },
                        {
                            "type": "command",
                            "command": (
                                "_MNG_HOOK_INPUT=$(cat);"
                                ' _MNG_NEW_SID=$(echo "$_MNG_HOOK_INPUT" | jq -r ".session_id // empty");'
                                ' if [ -z "$_MNG_NEW_SID" ]; then'
                                ' echo "mng: SessionStart hook failed to extract session_id from hook input: $_MNG_HOOK_INPUT" >&2;'
                                " exit 1;"
                                " fi;"
                                ' _MNG_SOURCE=$(echo "$_MNG_HOOK_INPUT" | jq -r ".source // empty");'
                                ' echo "$_MNG_NEW_SID" > "$MNG_AGENT_STATE_DIR/claude_session_id.tmp"'
                                ' && mv "$MNG_AGENT_STATE_DIR/claude_session_id.tmp" "$MNG_AGENT_STATE_DIR/claude_session_id";'
                                ' echo "$_MNG_NEW_SID${_MNG_SOURCE:+ $_MNG_SOURCE}" >> "$MNG_AGENT_STATE_DIR/claude_session_id_history"'
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
                            "command": """touch "$MNG_AGENT_STATE_DIR/active" && rm -f "$MNG_AGENT_STATE_DIR/permissions_waiting" && mkdir -p $MNG_HOST_DIR/events/mng/activity && echo '{"source": "mng/activity", "type": "activity", "event_id": "'"evt-$(head -c 16 /dev/urandom | xxd -p)"'", "timestamp": "'"$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z")"'"}' >> $MNG_HOST_DIR/events/mng/activity/events.jsonl""",
                        },
                        {
                            "type": "command",
                            "command": "tmux wait-for -S \"mng-submit-$(tmux display-message -p '#S')\" 2>/dev/null || true",
                        },
                    ]
                }
            ],
            "PermissionRequest": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'touch "$MNG_AGENT_STATE_DIR/permissions_waiting"',
                        },
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'rm -f "$MNG_AGENT_STATE_DIR/permissions_waiting"',
                        },
                    ],
                }
            ],
            "PostToolUseFailure": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'rm -f "$MNG_AGENT_STATE_DIR/permissions_waiting"',
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
                            "command": """rm -f "$MNG_AGENT_STATE_DIR/active" "$MNG_AGENT_STATE_DIR/permissions_waiting" && mkdir -p $MNG_HOST_DIR/events/mng/activity && echo '{"source": "mng/activity", "type": "activity", "event_id": "'"evt-$(head -c 16 /dev/urandom | xxd -p)"'", "timestamp": "'"$(date -u +"%Y-%m-%dT%H:%M:%S.000000000Z")"'"}' >> $MNG_HOST_DIR/events/mng/activity/events.jsonl""",
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


def merge_hooks_config(existing_settings: dict[str, Any], hooks_config: dict[str, Any]) -> dict[str, Any] | None:
    """Merge new hooks into existing settings, skipping duplicates.

    Returns the merged settings dict if any hooks were added, or None if all
    hooks already existed. Does not mutate the input dict.
    """
    merged = copy.deepcopy(existing_settings)
    if "hooks" not in merged:
        merged["hooks"] = {}

    any_added = False
    for event_name, event_hooks in hooks_config["hooks"].items():
        if event_name not in merged["hooks"]:
            merged["hooks"][event_name] = []

        for new_hook in event_hooks:
            if not hook_already_exists(merged["hooks"][event_name], new_hook):
                merged["hooks"][event_name].append(new_hook)
                any_added = True

    return merged if any_added else None
