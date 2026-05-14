"""Read/write helpers for Gemini CLI's settings.json.

Gemini CLI consults three settings tiers (highest-to-lowest precedence):
  1. system -- default ``/etc/gemini-cli/settings.json``, relocatable via the
     ``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` env var
  2. ``<project>/.gemini/settings.json`` (workspace)
  3. ``~/.gemini/settings.json`` (user)

The JSON shape produced by the merge and builder helpers here was validated
against Gemini CLI's published ``settings.schema.json`` and against a live
Gemini CLI 0.42.0 session. ``mngr_gemini`` writes a per-agent settings file
into the agent state dir and points Gemini at it via
``GEMINI_CLI_SYSTEM_SETTINGS_PATH``, keeping the user's workspace and
``~/.gemini/`` untouched.

Workspace trust (``--skip-trust`` replacement) is also relevant here: smoke-
testing established that the persistent trust file is
``~/.gemini/trustedFolders.json`` (a flat ``{ "<path>": "TRUST_FOLDER" }``
map), that the ``GEMINI_CLI_TRUST_WORKSPACE=true`` env var is Gemini's
documented headless-automation equivalent, and that ``--skip-trust`` is
silently weaker than either of the above (tools run, but workspace hooks
are stripped). ``GeminiDirectoryNotTrustedError`` is defined here so callers
can already reference the type.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.mngr.errors import ConfigError
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.file_utils import read_json_dict


class GeminiDirectoryNotTrustedError(ConfigError):
    """The source directory is not trusted in Gemini's settings.

    Gemini CLI shows a "Do you trust this folder?" dialog on first run in any
    new directory. When mngr launches Gemini and then sends keystrokes via
    tmux, those keystrokes accept the dialog and are consumed, so the intended
    initial prompt is lost AND the directory is silently trusted. The
    automated launch path clears this gate by setting
    ``GEMINI_CLI_TRUST_WORKSPACE=true`` on the agent's environment (see
    ``GeminiAgent.modify_env_vars``); this error type is raised by callers
    that detect an untrusted state outside that flow (e.g. interactive
    launches that cannot or choose not to set the env var).
    """

    def __init__(self, source_path: str) -> None:
        self.source_path = source_path
        super().__init__(
            f"Source directory {source_path} is not trusted by Gemini CLI. "
            "Run `mngr create` interactively (without --no-connect) to be prompted, "
            f"or run Gemini CLI manually in {source_path} and accept the trust dialog."
        )


# =============================================================================
# Config directory + settings path resolution
# =============================================================================


def get_gemini_config_dir() -> Path:
    """Return the Gemini CLI config directory.

    Gemini CLI does not currently expose an env var to relocate this directory
    (unlike Claude Code's ``$CLAUDE_CONFIG_DIR``). Returns ``~/.gemini/``.
    """
    return Path.home() / ".gemini"


def get_user_gemini_settings_path() -> Path:
    """Return the user-scope ``settings.json`` path: ``~/.gemini/settings.json``."""
    return get_gemini_config_dir() / "settings.json"


def get_project_gemini_settings_path(project_dir: Path) -> Path:
    """Return the workspace-scope ``settings.json`` path: ``<project>/.gemini/settings.json``."""
    return project_dir / ".gemini" / "settings.json"


def get_system_gemini_settings_path() -> Path:
    """Return the system-scope ``settings.json`` path: ``/etc/gemini-cli/settings.json``."""
    return Path("/etc/gemini-cli/settings.json")


# =============================================================================
# Atomic read / write
# =============================================================================


def read_gemini_settings(settings_path: Path) -> dict[str, Any]:
    """Read a Gemini ``settings.json`` file.

    Missing or empty files return ``{}``. Malformed JSON is logged and treated
    as ``{}`` (matching the behavior of ``mngr.utils.file_utils.read_json_dict``)
    so that a user typo in their settings does not break agent provisioning.
    """
    return read_json_dict(settings_path)


def write_gemini_settings(settings_path: Path, settings: Mapping[str, Any]) -> None:
    """Atomically write ``settings`` to ``settings_path`` with a ``.bak`` backup.

    Creates a ``settings.json.bak`` backup of the existing file (if any) before
    writing.
    """
    if settings_path.exists():
        backup_path = settings_path.parent / (settings_path.name + ".bak")
        shutil.copy2(settings_path, backup_path)
        logger.trace("Created backup of Gemini settings at {}", backup_path)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(settings_path, json.dumps(dict(settings), indent=2) + "\n")


# =============================================================================
# Env-var interpolation
# =============================================================================

# Matches ``$VAR``, ``${VAR}``, and ``${VAR:-default}`` in a Gemini settings
# string value, mirroring the syntax documented in the settings reference.
# Group 1 = bare ``$VAR``; group 2 = braced ``${VAR}`` or ``${VAR:-default}``;
# group 3 = the ``:-default`` tail (including the leading ``:-``) when present.
_ENV_VAR_PATTERN: Final = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)(:-[^}]*)?\})")


def _resolve_env_var_match(match: re.Match[str], env: Mapping[str, str]) -> str:
    """Resolve a single ``_ENV_VAR_PATTERN`` match against ``env``.

    The pattern guarantees exactly one of group 1 (bare) and group 2 (braced)
    matched, so ``name`` is never ``None`` in practice.
    """
    bare, braced, default_tail = match.group(1), match.group(2), match.group(3)
    name = bare or braced
    assert name is not None
    if name in env:
        return env[name]
    if default_tail is not None:
        return default_tail[2:]
    return match.group(0)


@pure
def interpolate_env_vars(value: str, env: Mapping[str, str]) -> str:
    """Resolve ``$VAR``, ``${VAR}``, and ``${VAR:-default}`` references in ``value``.

    Matches Gemini CLI's documented settings-value substitution. When the
    variable is defined in ``env``, the reference is replaced with its value.
    When it is not defined and a ``:-default`` suffix is present, the default
    is used. Otherwise the reference is left literal.

    Substitution is a single non-recursive pass: ``${A}`` resolving to
    ``${B}`` does not then resolve ``${B}``. This matches the conservative
    behavior most shells use for nested expansion in config files and prevents
    infinite loops on cyclic references.
    """
    return _ENV_VAR_PATTERN.sub(lambda m: _resolve_env_var_match(m, env), value)


# =============================================================================
# Hook config builders
# =============================================================================

# Names of the Gemini hook events used by ``mngr_gemini``. See
# https://github.com/google-gemini/gemini-cli/blob/main/docs/hooks/reference.md
# for the full list.
HOOK_EVENT_SESSION_START: Final = "SessionStart"
HOOK_EVENT_BEFORE_TOOL: Final = "BeforeTool"


@pure
def build_readiness_hooks_config() -> dict[str, Any]:
    """Build a ``hooks`` block whose ``SessionStart`` hook touches a readiness sentinel.

    ``mngr_gemini`` uses the resulting ``$MNGR_AGENT_STATE_DIR/session_started``
    file to detect that the Gemini TUI has finished starting up, rather than
    polling the rendered TUI for a banner. The hook command runs in a shell so
    the env var expands at hook-execution time.

    Gemini's ``SessionStart`` event is advisory: the ``decision`` field in the
    hook's stdout JSON is ignored, so this hook cannot block startup the way
    Claude Code's ``SessionStart`` can. That gap is acceptable for readiness
    signaling but means startup-time gates (e.g. trust enforcement) must live
    elsewhere.

    ``mngr_gemini`` installs this hook at the system tier (via
    ``GEMINI_CLI_SYSTEM_SETTINGS_PATH`` in the agent's env) rather than at
    the workspace tier. That keeps the user's workspace and ``~/.gemini/``
    untouched. The hook command runs in a shell at hook-execution time, so
    ``$MNGR_AGENT_STATE_DIR`` expands then -- there's nothing to interpolate
    at provisioning time.
    """
    return {
        "hooks": {
            HOOK_EVENT_SESSION_START: [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'touch "$MNGR_AGENT_STATE_DIR/session_started"',
                        },
                    ],
                }
            ],
        }
    }


@pure
def build_permission_auto_allow_hooks_config() -> dict[str, Any]:
    """Build a ``hooks`` block that auto-allows every tool use via ``BeforeTool``.

    Emits a ``BeforeTool`` hook with a wildcard ``.*`` matcher whose stdout
    JSON returns ``decision: "allow"``, mirroring how ``mngr_claude`` auto-
    accepts ``PermissionRequest`` dialogs.

    This is the explicit alternative to ``--approval-mode yolo``. Prefer this
    when you want a deterministic, regex-scoped allow that survives future
    Gemini CLI changes to the approval-mode hierarchy. Note GitHub issue
    google-gemini/gemini-cli#20469: in non-interactive ``autoEdit`` mode some
    policy-engine rules are bypassed; ``BeforeTool`` hooks fire regardless,
    which is part of why this approach is preferred.
    """
    return {
        "hooks": {
            HOOK_EVENT_BEFORE_TOOL: [
                {
                    "matcher": ".*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'echo \'{"decision":"allow"}\'',
                        }
                    ],
                }
            ],
        }
    }


# =============================================================================
# Merge helpers
# =============================================================================


@pure
def hook_already_exists(existing_hooks: list[dict[str, Any]], new_hook: dict[str, Any]) -> bool:
    """Return True if a matcher-group with the same set of inner commands exists.

    Compares the set of inner ``hooks[*].command`` strings so that two matcher
    groups with the same commands (in any order) are treated as duplicates.
    """
    new_commands = {h.get("command") for h in new_hook.get("hooks", [])}
    for existing in existing_hooks:
        existing_commands = {h.get("command") for h in existing.get("hooks", [])}
        if new_commands == existing_commands:
            return True
    return False


def merge_hooks_config(existing_settings: Mapping[str, Any], hooks_config: Mapping[str, Any]) -> dict[str, Any] | None:
    """Merge ``hooks_config`` into ``existing_settings``, skipping duplicate matcher groups.

    Returns the merged dict (a deep copy of ``existing_settings`` with the new
    hooks appended) when at least one matcher group was added, or ``None`` when
    every group already existed. Does not mutate the inputs.
    """
    merged: dict[str, Any] = copy.deepcopy(dict(existing_settings))
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
