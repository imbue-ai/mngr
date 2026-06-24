from __future__ import annotations

import copy
import getpass
import hashlib
import importlib.resources
import json
import os
import random
import shlex
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Annotated
from typing import Any
from typing import Callable
from typing import ClassVar
from typing import Final

import click
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.agents.base_agent import quote_agent_args
from imbue.mngr.agents.common_transcript import maybe_provision_common_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_raw_transcript_scripts
from imbue.mngr.agents.common_transcript import provision_scripts_to_commands_dir
from imbue.mngr.agents.installation import ensure_cli_installed
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import send_enter_via_tmux_wait_for_hook
from imbue.mngr.agents.update_policy import AgentUpdatePolicy
from imbue.mngr.agents.update_policy import is_self_update_disabled
from imbue.mngr.api.preservation import PreservedItem
from imbue.mngr.api.preservation import adopt_sessions
from imbue.mngr.api.preservation import dedupe_by_resolved_path
from imbue.mngr.api.preservation import iter_agent_session_paths
from imbue.mngr.api.preservation import preserve_agent_state
from imbue.mngr.api.preservation import preserve_host_agents_on_destroy
from imbue.mngr.api.preservation import require_unique_match
from imbue.mngr.api.preservation import run_adopt_session_preflight
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.external_settings import apply_settings_patch
from imbue.mngr.config.field_markers import SettingsPatchField
from imbue.mngr.errors import AgentInstallationError
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import ConfigError
from imbue.mngr.errors import NoCommandDefinedError
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import classify_waiting_reason
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.common import is_macos
from imbue.mngr.hosts.file_upload import upload_files_in_bulk
from imbue.mngr.hosts.host import write_json_dict_via_host
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import CliBackedAgentMixin
from imbue.mngr.interfaces.agent import HasAutoInstallMixin
from imbue.mngr.interfaces.agent import HasCommonTranscriptMixin
from imbue.mngr.interfaces.agent import HasSessionAdoptionMixin
from imbue.mngr.interfaces.agent import HasSessionPreservationMixin
from imbue.mngr.interfaces.agent import HasUnattendedModeMixin
from imbue.mngr.interfaces.agent import HasVersionManagementMixin
from imbue.mngr.interfaces.agent import SupportsLiveOutputMixin
from imbue.mngr.interfaces.data_types import FileTransferSpec
from imbue.mngr.interfaces.data_types import FileType
from imbue.mngr.interfaces.data_types import RelativePath
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import HostInterface
from imbue.mngr.interfaces.host import HostLocation
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.live_output import LiveOutputReader
from imbue.mngr.plugins.hookspecs import OnBeforeCreateArgs
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import TransferMode
from imbue.mngr.primitives import WaitingReason
from imbue.mngr.utils.git_utils import find_git_source_path
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_claude import hookimpl
from imbue.mngr_claude import resources as _claude_resources
from imbue.mngr_claude.claude_config import ClaudeDirectoryNotTrustedError
from imbue.mngr_claude.claude_config import ClaudeEffortCalloutNotDismissedError
from imbue.mngr_claude.claude_config import ClaudeOnboardingNotCompletedError
from imbue.mngr_claude.claude_config import MANAGED_SETTINGS_RELATIVE_PATH
from imbue.mngr_claude.claude_config import acknowledge_cost_threshold
from imbue.mngr_claude.claude_config import add_claude_trust_for_path
from imbue.mngr_claude.claude_config import auto_dismiss_claude_dialogs
from imbue.mngr_claude.claude_config import build_credential_sync_hooks_config
from imbue.mngr_claude.claude_config import build_permission_auto_allow_hooks_config
from imbue.mngr_claude.claude_config import build_readiness_hooks_config
from imbue.mngr_claude.claude_config import check_claude_dialogs_dismissed
from imbue.mngr_claude.claude_config import complete_onboarding
from imbue.mngr_claude.claude_config import dismiss_effort_callout
from imbue.mngr_claude.claude_config import encode_claude_project_dir_name
from imbue.mngr_claude.claude_config import find_project_config
from imbue.mngr_claude.claude_config import find_user_config_in_isolated_mode
from imbue.mngr_claude.claude_config import find_user_config_in_unisolated_mode
from imbue.mngr_claude.claude_config import fold_hook_configs
from imbue.mngr_claude.claude_config import get_agent_claude_config_dir
from imbue.mngr_claude.claude_config import get_agent_claude_plugin_dir
from imbue.mngr_claude.claude_config import get_claude_config_dir
from imbue.mngr_claude.claude_config import get_managed_settings_path
from imbue.mngr_claude.claude_config import get_user_claude_config_dir
from imbue.mngr_claude.claude_config import is_effort_callout_dismissed
from imbue.mngr_claude.claude_config import is_onboarding_completed
from imbue.mngr_claude.claude_config import is_source_directory_trusted
from imbue.mngr_claude.claude_config import read_claude_config
from imbue.mngr_claude.claude_config import remove_claude_trust_for_path
from imbue.mngr_claude.claude_config import resolve_shared_claude_config_dir
from imbue.mngr_claude.stream_buffer import SnapshotDeltaReader

_READY_SIGNAL_TIMEOUT_SECONDS: Final[float] = 10.0

# Paths within ~/.claude/ to sync to the per-agent config dir.
# Used by both get_files_for_deploy() and provision() to ensure consistency.
_CLAUDE_HOME_SYNC_DIRS: Final[tuple[str, ...]] = ("skills", "agents", "commands", "plugins")

# Subset of _CLAUDE_HOME_SYNC_DIRS synced via child-level symlinks (one symlink per
# child) instead of a single dir-level symlink, so the per-agent config dir can hold
# its own real files alongside the shared source: plugins/ for generated config files,
# skills/ for a skill-provisioned agent's own primary skill.
_CLAUDE_HOME_CHILD_SYMLINK_DIRS: Final[tuple[str, ...]] = ("skills", "plugins")

# Individual files from ~/.claude/ to sync (not generated/transformed).
# settings.json is handled separately by _build_settings_json.
_CLAUDE_HOME_SYNC_FILES: Final[tuple[str, ...]] = ("keybindings.json",)

_INSTALLED_PLUGINS_RELATIVE_PATH: Final[Path] = Path("plugins") / "installed_plugins.json"
_KNOWN_MARKETPLACES_RELATIVE_PATH: Final[Path] = Path("plugins") / "known_marketplaces.json"

_INSTALLED_PLUGINS_SENTINEL_PREFIX: Final[str] = "/__mngr_plugins_source__"
"""Sentinel prefix written into plugin/marketplace path values at deploy build time.

At build time, ``get_files_for_deploy`` rewrites absolute local paths
(e.g. /home/user/.claude/plugins/cache/...) to use this sentinel prefix
in both installed_plugins.json (installPath) and known_marketplaces.json
(installLocation).  At runtime, ``_resolve_plugins_dir_sentinel``
rewrites the sentinel to the actual per-agent config dir.  This avoids
depending on the build machine's home directory path.
"""


# An mngr agent's isolated Claude config dir lives at
# <agent_state_dir>/plugin/claude/anthropic/ (the per-agent replacement for ~/.claude/),
# with session JSONLs filed under its projects/ subdir. Both live local mngr agents and
# preserved agents mirror this layout, so --adopt can resolve a session ID against
# either. Derived from the single-source ``get_agent_claude_config_dir`` (passing an empty
# base yields the agent-relative subpath) so this layout never drifts from it.
_AGENT_CLAUDE_CONFIG_RELPATH: Final[Path] = get_agent_claude_config_dir(Path())
_AGENT_CLAUDE_PROJECTS_RELPATH: Final[Path] = _AGENT_CLAUDE_CONFIG_RELPATH / "projects"


def _mngr_session_projects_dirs(mngr_ctx: MngrContext) -> list[Path]:
    """Return the per-agent Claude ``projects`` directories on the local host.

    Scans both live local mngr agents (``<host_dir>/agents/<id>/...``) and
    preserved agents (``<host_dir>/preserved/<name>--<id>/...``; see
    ``preserve_sessions_on_destroy``), each of which stores its session JSONLs
    at ``plugin/claude/anthropic/projects/<encoded-work-dir>/``.

    Only the local host dir is scanned: an adopted session's files are copied
    onto the destination host from a path that must already be reachable as a
    local source, so remote agents' session dirs are not searched here.
    """
    local_host_dir = Path(mngr_ctx.config.default_host_dir).expanduser()
    return iter_agent_session_paths(local_host_dir, _AGENT_CLAUDE_PROJECTS_RELPATH)


def _resolve_adopt_session(adopt_session_arg: str, mngr_ctx: MngrContext) -> tuple[str, Path]:
    """Resolve an --adopt argument to a (session_id, project_dir) pair.

    Accepts either:
    - A path to a .jsonl file (e.g. ~/.claude/projects/foo/abc123.jsonl)
    - A session ID string, searched across (all of):
      * the current config dir's ``projects/`` ($CLAUDE_CONFIG_DIR or ~/.claude)
      * the user-scope ``~/.claude/projects/``
      * every live local mngr agent's per-agent ``projects/`` dir
      * every preserved agent's ``projects/`` dir (preserve_sessions_on_destroy)

      All of these dirs are searched; a session ID matching in more than one is
      rejected as ambiguous (the user must pass the full ``.jsonl`` path).

    Returns (session_id, source_project_dir).
    """
    if adopt_session_arg.endswith(".jsonl"):
        session_file = Path(adopt_session_arg).resolve()
        if not session_file.exists():
            raise UserInputError(f"Session file not found: {session_file}")
        return session_file.stem, session_file.parent

    # Search the current config dir, the user-scope dir, and every live local
    # mngr agent and preserved agent (all of them -- a session ID matching in
    # multiple dirs is treated as ambiguous below, not resolved by order).
    # Inside an mngr agent CLAUDE_CONFIG_DIR points to the agent's isolated
    # config dir while the user's sessions live in the user-scope dir.
    current_config_dir = get_claude_config_dir()
    user_config_dir = get_user_claude_config_dir()

    candidate_dirs = [current_config_dir / "projects", user_config_dir / "projects"]
    candidate_dirs.extend(_mngr_session_projects_dirs(mngr_ctx))
    search_dirs = dedupe_by_resolved_path(candidate_dirs)

    matches: list[Path] = []
    for projects_dir in search_dirs:
        if projects_dir.exists():
            matches.extend(projects_dir.glob(f"*/{adopt_session_arg}.jsonl"))

    # Don't enumerate the searched dirs in the not-found message: there is one per local mngr
    # agent, so the list can run to hundreds of paths. The --adopt help documents the search
    # scope (current/user Claude config dirs, live agents, preserved agents).
    match = require_unique_match(
        matches,
        not_found_message=(
            f"Session {adopt_session_arg} not found. "
            "Check that the session ID is correct, or pass a path to the .jsonl file."
        ),
        ambiguous_message=(
            f"Session {adopt_session_arg} found in multiple project directories; "
            "pass the full path to the .jsonl file to specify which one:"
        ),
    )
    return adopt_session_arg, match.parent


class ClaudeAgentConfig(AgentTypeConfig):
    """Config for the claude agent type."""

    command: CommandString = Field(
        default=CommandString("claude"),
        description="Command to run claude agent",
    )
    sync_home_settings: bool = Field(
        default=True,
        description="Whether to sync Claude settings from ~/.claude/ to the per-agent config dir",
    )
    sync_claude_json: bool = Field(
        default=True,
        description="Whether to sync the local ~/.claude.json to a remote host (useful for API key settings and permissions)",
    )
    sync_repo_settings: bool = Field(
        default=True,
        description="Whether to sync unversioned .claude/ settings from the repo to the agent work_dir",
    )
    sync_claude_credentials: bool = Field(
        default=True,
        description="Whether to sync the local ~/.claude/.credentials.json to the per-agent config dir",
    )
    override_settings_folder: Path | None = Field(
        default=None,
        description="Extra folder to sync to the repo .claude/ folder in the agent work_dir."
        "(files are transferred after user settings, so they can override)",
    )
    symlink_user_resources: bool = Field(
        default=True,
        description="Whether to symlink (True) or copy (False) user resources from ~/.claude/ "
        "into local per-agent config dirs. Symlinks avoid duplication and keep the "
        "per-agent dir lightweight; copies provide full isolation.",
    )
    convert_macos_credentials: bool = Field(
        default=True,
        description="Whether to convert macOS keychain credentials to flat files for remote hosts",
    )
    sync_credentials_on_login: bool = Field(
        default=True,
        description="Whether credential changes should propagate across sessions. "
        "On macOS, installs a hook to sync keychain entries after login. "
        "On Linux, symlinks (True) or copies (False) .credentials.json.",
    )
    check_installation: bool = Field(
        default=True,
        description="Check if claude is installed (if False, assumes it is already present)",
    )
    version: str | None = Field(
        default=None,
        description="Pin the Claude Code version to install (e.g., '2.1.50'). "
        "When set, installation uses this specific version and provisioning verifies the installed version matches. "
        'If None, uses the latest available version. Pin alongside `update_policy = "NEVER"` to keep the '
        "binary on the pinned version (claude's auto-updater would otherwise move it off the pin).",
    )
    update_policy: AgentUpdatePolicy | None = Field(
        default=None,
        description="How to handle Claude Code's background auto-updater. NEVER sets DISABLE_AUTOUPDATER=1 "
        "in the agent environment so the binary stays on the installed version; AUTO leaves the auto-updater "
        "enabled. ASK has no interactive flow for claude and behaves like AUTO. When unset (the default), "
        "resolves to NEVER (auto-update disabled) so a managed agent stays on its installed version -- set "
        "AUTO to opt back into Claude Code's auto-updater. Ignored when isolate_local_config_dir=False "
        "(shared) mode.",
    )
    auto_dismiss_dialogs: bool = Field(
        default=False,
        description="Automatically dismiss all Claude startup dialogs (trust, effort callout, onboarding) "
        "before startup. When False, the interactive flow prompts.",
    )
    auto_allow_permissions: bool = Field(
        default=False,
        description="When True, adds a PermissionRequest hook that auto-allows all permission dialogs. "
        "This means Claude Code will never pause for permission approval.",
    )
    settings_overrides: Annotated[dict[str, Any], SettingsPatchField()] = Field(
        default_factory=dict,
        description="A patch merged onto your home Claude settings at provisioning. A bare key assigns "
        "(narrowing-checked); to merge onto the base instead (or replace without the narrowing check), "
        'declare the key in a top-level `__mngr_merge` map -- `__mngr_merge = {"permissions.allow" = '
        '"extend"}` -- which vanilla Claude Code ignores. The `__extend`/`__assign` leaf suffixes are '
        "not accepted here (they would leak into settings.json as junk Claude keys). See mngr's config README.",
    )
    emit_common_transcript: bool = Field(
        default=True,
        description="Emit a common, agent-agnostic transcript alongside the raw Claude transcript. "
        "When enabled, a background process converts raw transcript events into a common format at "
        "events/claude/common_transcript/events.jsonl. The common format includes user messages, "
        "assistant messages, and tool call/result summaries.",
    )
    preserve_sessions_on_destroy: bool = Field(
        default=True,
        description="When destroying this agent, first copy its transcripts and resumable session "
        "store to <local_host_dir>/preserved/ so they survive. Set to False to discard them.",
    )
    isolate_local_config_dir: bool = Field(
        default=True,
        description="When True (the default), provision a per-agent Claude config dir so each local agent is "
        "isolated and mngr never has to touch your default Claude config. When False, share the user's "
        "$CLAUDE_CONFIG_DIR across all claude agents instead of provisioning a per-agent config dir. In shared "
        "mode mngr still writes to your default Claude config to dismiss the cosmetic startup dialogs (trust, "
        "onboarding, effort callout, cost threshold) -- honoring auto_dismiss_dialogs -- so they don't intercept "
        "automated input; it never accepts bypass-permissions mode there (that is handled via settings.json). "
        "Credentials stay in sync (which is what Claude subscriptions on macOS need). Only meaningful for local "
        "hosts: a non-local agent always uses an isolated config dir (the user's config and keychain live on the "
        "local machine), so this flag is ignored for remote agents. See the imbue-mngr-claude README for the "
        "shared-mode setup requirements and reduced-support limitations (including the `--settings` collision).",
    )
    use_env_config_dir: bool | None = Field(
        default=None,
        description="DEPRECATED: the old name for the inverse of isolate_local_config_dir; set "
        "isolate_local_config_dir instead. use_env_config_dir=true means the same as "
        "isolate_local_config_dir=false (share the user's $CLAUDE_CONFIG_DIR), and use_env_config_dir=false "
        "means isolate_local_config_dir=true (per-agent config dir). When set, mngr emits a deprecation "
        "warning; setting both keys to non-inverse values (e.g. both true) is an error.",
    )
    streaming_snapshot_interval_seconds: float = Field(
        default=0.0,
        description="Poll interval (in seconds) for the tmux-based response-streaming watcher. When > 0, a "
        "background watcher periodically captures the agent's tmux pane, reverse-maps the rendered "
        "assistant text back into markdown, and writes it to "
        "$MNGR_AGENT_STATE_DIR/plugin/claude/stream_buffer. When <= 0 (the default), the watcher is "
        "not provisioned or run.",
    )

    def resolve_isolate_local_config_dir(self) -> bool:
        """Return the effective ``isolate_local_config_dir``, reconciling the deprecated alias.

        ``use_env_config_dir`` is the deprecated inverse of ``isolate_local_config_dir``
        (``use_env_config_dir=true`` == ``isolate_local_config_dir=false``). When only
        the deprecated key is set, its inverse is used. When both are set they must be
        consistent inverses; setting them to the same value (e.g. both ``true``) is
        contradictory and raises. The deprecation warning is emitted separately (once,
        at provisioning time) rather than here, since this is called on every access.
        """
        if self.use_env_config_dir is None:
            return self.isolate_local_config_dir
        if "isolate_local_config_dir" in self.model_fields_set and (
            self.isolate_local_config_dir == self.use_env_config_dir
        ):
            raise ConfigError(
                "Contradictory Claude config: `isolate_local_config_dir` and the deprecated "
                "`use_env_config_dir` are inverses of each other, but both were set to the same value "
                f"(isolate_local_config_dir={self.isolate_local_config_dir}, "
                f"use_env_config_dir={self.use_env_config_dir}). Set only `isolate_local_config_dir`."
            )
        return not self.use_env_config_dir


def build_mngr_hook_configs(config: ClaudeAgentConfig, *, is_unattended: bool) -> list[dict[str, Any]]:
    """Build the list of mngr hook configs to fold into a Claude settings file.

    Always includes the readiness hooks. Adds the macOS keychain credential-sync
    hook when ``sync_credentials_on_login`` is set (on macOS), and the permission
    auto-allow hook when the agent runs unattended. The caller passes
    ``is_unattended`` from the ``HasUnattendedModeMixin`` contract
    (``is_unattended_enabled``) so the gate cannot drift from that contract; the
    agent-less deploy path passes the equivalent config field directly.
    """
    hook_configs = [build_readiness_hooks_config()]
    if config.sync_credentials_on_login and is_macos():
        hook_configs.append(build_credential_sync_hooks_config())
    if is_unattended:
        hook_configs.append(build_permission_auto_allow_hooks_config())
    return hook_configs


def _has_settings_flag(args: Sequence[str]) -> bool:
    """True if ``args`` contains a claude ``--settings`` flag (``--settings X`` or ``--settings=X``).

    Detection only looks at the flag token, so it works for both the quote-retaining
    ``cli_args`` tokens and the raw ``agent_args`` argv.
    """
    return any(arg == "--settings" or arg.startswith("--settings=") for arg in args)


class ProvisioningContext(FrozenModel):
    """Runtime context derived from host type and transfer mode."""

    is_unattended: bool = Field(description="Agent runs without user interaction (remote/deploy)")
    is_auto_approve: bool = Field(
        default=False,
        description="The human auto-approved mngr's prompts (`mngr create --yes`). Dismisses first-run "
        "*dialogs* (onboarding/effort/trust), but -- unlike `is_unattended` -- does not accept "
        "bypass-permissions mode (tool auto-allow is governed by `auto_allow_permissions`).",
    )
    copy_project_config_from: Path | None = Field(
        default=None, description="Source dir to copy project config from (worktree mode)"
    )


_ALWAYS_CLAUDE_JSON_FLAGS: Final[Mapping[str, bool]] = {"hasAcknowledgedCostThreshold": True}
# First-run *dialog* dismissals (cosmetic startup prompts). Dismissed for an unattended agent
# OR when the human auto-approved mngr's prompts (--yes) -- neither changes tool permissions.
_DIALOG_DISMISS_CLAUDE_JSON_FLAGS: Final[Mapping[str, bool]] = {
    "effortCalloutDismissed": True,
    "hasCompletedOnboarding": True,
}
# Accepting bypass-permissions mode is a tool-permission change, so it applies only to a
# genuinely unattended (remote/deploy) agent -- NOT merely because the human passed --yes.
_PERMISSION_CLAUDE_JSON_FLAGS: Final[Mapping[str, bool]] = {"bypassPermissionsModeAccepted": True}
_UNATTENDED_SETTINGS_FLAGS: Final[Mapping[str, Any]] = {
    "skipDangerousModePermissionPrompt": True,
    # fastMode off by default in unattended mode (API limitation)
    "fastMode": False,
}


@pure
def compute_claude_json_flags(ctx: ProvisioningContext) -> Mapping[str, bool]:
    """Compute .claude.json flags based on provisioning context.

    Cosmetic first-run dialogs are dismissed when unattended OR when the human auto-approved
    mngr's prompts (--yes); the bypass-permissions-mode acceptance is added only when unattended.
    """
    flags = dict(_ALWAYS_CLAUDE_JSON_FLAGS)
    if ctx.is_unattended or ctx.is_auto_approve:
        flags.update(_DIALOG_DISMISS_CLAUDE_JSON_FLAGS)
    if ctx.is_unattended:
        flags.update(_PERMISSION_CLAUDE_JSON_FLAGS)
    return flags


@pure
def compute_settings_json_flags(ctx: ProvisioningContext) -> Mapping[str, Any]:
    """Compute settings.json flags based on provisioning context.

    These govern tool-permission behavior (skip the dangerous-mode prompt), so they apply only
    to an unattended agent -- not on a bare --yes, which auto-approves prompts but must not
    silently change tool permissions.
    """
    if ctx.is_unattended:
        return dict(_UNATTENDED_SETTINGS_FLAGS)
    return {}


@pure
def should_trust_work_dir(config: ClaudeAgentConfig, ctx: ProvisioningContext) -> bool:
    """Determine whether work_dir should be auto-trusted (a dialog-consent decision)."""
    return ctx.is_unattended or ctx.is_auto_approve or config.auto_dismiss_dialogs


_MNGR_AGENT_CONFIG_DIR_MARKER: Final[str] = f"/{_AGENT_CLAUDE_CONFIG_RELPATH.as_posix()}/"
"""Path segment that identifies an mngr agent's Claude config directory.

Agent config dirs follow the pattern: <agent_state_dir>/plugin/claude/anthropic/.
Finding this segment in an installPath means the plugin was installed inside
an mngr agent rather than in the user's persistent ~/.claude/ directory.
"""

# The ``--settings`` flag passed to claude at launch (see ``assemble_command``),
# pointing at the per-agent managed settings file via the runtime
# $MNGR_AGENT_STATE_DIR. Loading mngr's hooks this way keeps them out of the
# project's settings.local.json. See ``get_managed_settings_path``.
_MANAGED_SETTINGS_SHELL_PATH: Final[str] = f"$MNGR_AGENT_STATE_DIR/{'/'.join(MANAGED_SETTINGS_RELATIVE_PATH)}"
MANAGED_SETTINGS_LAUNCH_ARG: Final[str] = f'--settings "{_MANAGED_SETTINGS_SHELL_PATH}"'


_PLUGINS_DIR_MARKER: Final[str] = "/plugins/"
"""Generic marker for extracting relative plugin paths.

Used as a fallback when the installPath doesn't start with the expected
source_claude_dir prefix. The path after the last '/plugins/' occurrence
is used as the relative path under the target config dir's plugins/ directory.
"""


def _rebase_plugin_path(path: str, source_claude_dir: Path, target_config_dir: Path) -> str | None:
    """Rebase an absolute plugin/marketplace path from source to target config dir.

    Returns the rebased path, or None if the path could not be rewritten
    (no '/plugins/' segment found). Handles two cases:
    1. Path starts with source_claude_dir -- direct prefix replacement.
    2. Path has a '/plugins/' segment -- best-effort extraction of the
       relative path after the last '/plugins/' occurrence.
    """
    source_prefix = str(source_claude_dir) + "/"
    if path.startswith(source_prefix):
        return str(target_config_dir / path[len(source_prefix) :])
    marker_idx = path.rfind(_PLUGINS_DIR_MARKER)
    if marker_idx != -1:
        relative = "plugins/" + path[marker_idx + len(_PLUGINS_DIR_MARKER) :]
        return str(target_config_dir / relative)
    return None


def _rewrite_installed_plugins_paths(content: str, source_claude_dir: Path, target_config_dir: Path) -> str:
    """Rewrite installPath values in installed_plugins.json for a target config dir.

    Rebases absolute paths from source_claude_dir onto target_config_dir
    so that Claude Code can find plugin files in the per-agent config dir.

    For paths that don't start with source_claude_dir, attempts a best-effort
    rewrite using the last '/plugins/' segment as a marker. Logs a warning
    with the expected persistent path so the user can fix their config.
    """
    data: dict[str, Any] = json.loads(content)
    source_prefix = str(source_claude_dir) + "/"
    installed_plugins_path = source_claude_dir / _INSTALLED_PLUGINS_RELATIVE_PATH
    for plugin_name, plugin_entries in data.get("plugins", {}).items():
        for entry in plugin_entries:
            install_path = entry.get("installPath", "")
            rewritten = _rebase_plugin_path(install_path, source_claude_dir, target_config_dir)
            if rewritten is not None:
                # Log warnings for best-effort rewrites (path didn't start with expected prefix).
                if not install_path.startswith(source_prefix):
                    expected_path = str(source_claude_dir / rewritten[len(str(target_config_dir)) + 1 :])
                    if _MNGR_AGENT_CONFIG_DIR_MARKER in install_path:
                        logger.warning(
                            "Plugin {!r} in {} has installPath pointing to a previous mngr agent's "
                            "config directory. Rewrote best-effort for remote provisioning.\n"
                            "  Current (stale): {}\n"
                            "  Expected:        {}\n"
                            "To fix, uninstall the plugin with '/plugin' and reinstall it.",
                            plugin_name,
                            installed_plugins_path,
                            install_path,
                            expected_path,
                        )
                    else:
                        logger.warning(
                            "Plugin {!r} in {} has installPath that does not start with {}. "
                            "Rewrote best-effort for remote provisioning.\n"
                            "  Current: {}\n"
                            "  Expected: {}\n"
                            "To fix, create a symlink under {}/plugins/cache/ pointing to "
                            "the local plugin directory, then update the installPath in {} "
                            "to use the symlink path.",
                            plugin_name,
                            installed_plugins_path,
                            source_prefix,
                            install_path,
                            expected_path,
                            source_claude_dir,
                            installed_plugins_path,
                        )
                entry["installPath"] = rewritten
            else:
                logger.warning(
                    "Plugin {!r} in {} has installPath {!r} that could not be rewritten "
                    "(no '{}' segment found). Keeping path as-is; the plugin may not "
                    "work on the remote host.",
                    plugin_name,
                    installed_plugins_path,
                    install_path,
                    _PLUGINS_DIR_MARKER,
                )
    return json.dumps(data, indent=2) + "\n"


def _build_settings_json(
    source_claude_dir: Path,
    config: ClaudeAgentConfig,
    ctx: ProvisioningContext,
    sync_local: bool,
    *,
    is_unattended: bool = False,
    allow_narrowing: bool = False,
) -> str:
    """Build settings.json content for per-agent config dirs.

    Builds the provision base ``B`` (home settings.json or generated defaults + context
    flags + mngr's own hooks), then folds the user's ``settings_overrides`` patch onto it
    via ``apply_settings_patch`` (see that function for the fold semantics and the narrowing
    guard, gated by ``allow_narrowing``).

    The hooks land in the config-dir ``settings.json`` (the "user" layer Claude reads from
    ``$CLAUDE_CONFIG_DIR``) rather than a managed ``--settings`` file, so a user's own
    ``--settings`` passes through and Claude layers it natively.
    """
    source = source_claude_dir / "settings.json"
    if sync_local and source.exists():
        try:
            data: dict[str, Any] = json.loads(source.read_text())
            base_description = f"your home Claude settings ({source})"
        except json.JSONDecodeError:
            logger.warning("Corrupt settings.json at {}, using defaults", source)
            data = _generate_claude_home_settings()
            base_description = "mngr's generated Claude settings defaults"
    else:
        data = _generate_claude_home_settings()
        base_description = "mngr's generated Claude settings defaults"
    # Flags are flat scalars, so a shallow update is correct here.
    data.update(compute_settings_json_flags(ctx))

    # Fold in mngr's own hooks (concatenated into the hook event lists by
    # merge_hooks_config), then fold the user's settings_overrides patch onto that base.
    data = fold_hook_configs(data, build_mngr_hook_configs(config, is_unattended=is_unattended))
    data = apply_settings_patch(
        data, config.settings_overrides, allow_narrowing=allow_narrowing, base_description=base_description
    )
    return json.dumps(data, indent=2) + "\n"


def _build_claude_json(
    *,
    work_dir: Path,
    config: ClaudeAgentConfig,
    ctx: ProvisioningContext,
    sync_local: bool,
    version: str | None,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    """Build .claude.json data for the per-agent config dir.

    Unified builder for local, remote, and deploy paths:
    1. Reads base config (global ~/.claude.json if sync_local, else generated defaults)
    2. Applies context-dependent flags (e.g. bypassPermissionsModeAccepted for unattended)
    3. Copies source project config to work_dir if ctx.copy_project_config_from is set
    4. Trusts work_dir if should_trust_work_dir(config, ctx)

    Returns the dict so callers can do further modifications (e.g. keychain merge)
    before serializing.
    """
    disable_auto_update = is_self_update_disabled(config.update_policy, is_unattended=ctx.is_unattended)
    if sync_local:
        local_config = read_claude_config(find_user_config_in_isolated_mode())
        data: dict[str, Any] = (
            local_config
            if local_config
            else _generate_claude_json(version, current_time=current_time, disable_auto_update=disable_auto_update)
        )
    else:
        data = _generate_claude_json(version, current_time=current_time, disable_auto_update=disable_auto_update)

    data.update(compute_claude_json_flags(ctx))

    projects = data.setdefault("projects", {})

    # Copy project config from source (worktree mode)
    if ctx.copy_project_config_from is not None:
        source_path = ctx.copy_project_config_from.resolve()
        # When sync_local=True, `projects` already holds the global projects (loaded above).
        # When sync_local=False, there are no global projects to search.
        source_config = find_project_config(projects if sync_local else {}, source_path)
        if source_config is not None:
            projects[str(source_path)] = source_config
            worktree_path_str = str(work_dir.resolve())
            if worktree_path_str not in projects:
                worktree_config = copy.deepcopy(source_config)
                worktree_config["_mngrCreated"] = True
                worktree_config["_mngrSourcePath"] = str(source_path)
                projects[worktree_path_str] = worktree_config

    # Trust work_dir if unattended or auto_dismiss_dialogs
    if should_trust_work_dir(config, ctx):
        projects.setdefault(str(work_dir.resolve()), {})["hasTrustDialogAccepted"] = True

    return data


def _generate_installed_plugins_content(source_claude_dir: Path, target_config_dir: Path) -> str | None:
    """Read installed_plugins.json from source and rewrite paths to target.

    Returns None if the file does not exist at source_claude_dir.
    """
    source_path = source_claude_dir / _INSTALLED_PLUGINS_RELATIVE_PATH
    if not source_path.exists():
        return None
    content = source_path.read_text()
    return _rewrite_installed_plugins_paths(content, source_claude_dir, target_config_dir)


def _rewrite_known_marketplaces_paths(content: str, source_claude_dir: Path, target_config_dir: Path) -> str:
    """Rewrite installLocation values in known_marketplaces.json for a target config dir.

    Rebases absolute paths from source_claude_dir onto target_config_dir so that
    Claude Code can find marketplace git clones in the per-agent config dir.
    Without this, Claude Code re-clones marketplaces from GitHub on startup and
    may invalidate plugin caches when the remote clone has a different HEAD.
    """
    data: dict[str, Any] = json.loads(content)
    for marketplace_name, marketplace_info in data.items():
        install_location = marketplace_info.get("installLocation", "")
        rewritten = _rebase_plugin_path(install_location, source_claude_dir, target_config_dir)
        if rewritten is not None:
            marketplace_info["installLocation"] = rewritten
        else:
            logger.warning(
                "Marketplace {!r} has installLocation {!r} that could not be rewritten "
                "(no '{}' segment found). Keeping path as-is; the marketplace may not "
                "work on the remote host.",
                marketplace_name,
                install_location,
                _PLUGINS_DIR_MARKER,
            )
    return json.dumps(data, indent=2) + "\n"


def _generate_known_marketplaces_content(source_claude_dir: Path, target_config_dir: Path) -> str | None:
    """Read known_marketplaces.json from source and rewrite paths to target.

    Returns None if the file does not exist at source_claude_dir.
    """
    source_path = source_claude_dir / _KNOWN_MARKETPLACES_RELATIVE_PATH
    if not source_path.exists():
        return None
    content = source_path.read_text()
    return _rewrite_known_marketplaces_paths(content, source_claude_dir, target_config_dir)


def _check_claude_installed(host: OnlineHostInterface) -> bool:
    """Check if claude is installed on the host."""
    result = host.execute_idempotent_command("command -v claude", timeout_seconds=10.0)
    return result.success


def _parse_claude_version_output(output: str) -> str | None:
    """Parse the version string from 'claude --version' output.

    Expected format: '2.1.50 (Claude Code)' -> '2.1.50'
    """
    stripped = output.strip()
    if not stripped:
        return None
    parts = stripped.split()
    return parts[0] if parts else None


def _get_claude_version(host: OnlineHostInterface) -> str | None:
    """Get the installed claude version on the host.

    Returns the version string (e.g., '2.1.50') or None if claude is not installed
    or the version cannot be determined.
    """
    result = host.execute_idempotent_command("claude --version", timeout_seconds=10.0)
    if not result.success:
        logger.debug("Failed to get claude version on host: {}", result.stderr)
        return None
    return _parse_claude_version_output(result.stdout)


def _get_local_claude_version(concurrency_group: ConcurrencyGroup) -> str | None:
    """Get the locally installed claude version.

    Returns the version string (e.g., '2.1.50') or None if claude is not installed locally.
    """
    try:
        result = concurrency_group.run_process_to_completion(
            ["claude", "--version"],
            is_checked_after=False,
        )
    except ProcessSetupError:
        logger.debug("claude binary not found locally")
        return None
    if result.returncode != 0:
        logger.debug("Failed to get local claude version (exit code {})", result.returncode)
        return None
    return _parse_claude_version_output(result.stdout)


def _build_install_command_hint(version: str | None = None) -> str:
    """Build the install command hint shown in user-facing messages."""
    if version:
        return f"curl -fsSL https://claude.ai/install.sh | bash -s {version}"
    return "curl -fsSL https://claude.ai/install.sh | bash"


CLAUDE_INSTALL_PATH: Final[str] = "$HOME/.local/bin"
"""Directory where the Claude Code installer places the claude binary."""


def _build_claude_install_command(version: str | None) -> str:
    """Build the official-installer shell command, pinning ``version`` when given."""
    version_arg = f" {shlex.quote(version)}" if version else ""
    steps = [
        "curl --version",
        "curl -fsSL https://claude.ai/install.sh -o /tmp/install_claude.sh",
        f"bash /tmp/install_claude.sh{version_arg}",
        "rm -f /tmp/install_claude.sh",
        f"test -x {CLAUDE_INSTALL_PATH}/claude",
        f"""grep -qF 'export PATH="{CLAUDE_INSTALL_PATH}:$PATH"' ~/.bashrc 2>/dev/null || echo 'export PATH="{CLAUDE_INSTALL_PATH}:$PATH"' >> ~/.bashrc""",
    ]
    return " && ".join(steps)


def _install_claude(host: OnlineHostInterface, version: str | None = None) -> None:
    """Install claude on the host using the official installer.

    Downloads the install script to a temp file, runs it, then verifies
    the binary exists. When version is specified, passes it as a positional
    argument (e.g., 'bash /tmp/install_claude.sh 2.1.50').
    """
    result = host.execute_idempotent_command(_build_claude_install_command(version), timeout_seconds=300.0)
    if not result.success:
        raise PluginMngrError(f"Failed to install claude. stderr: {result.stderr}")


def _prompt_user_for_installation(version: str | None = None) -> bool:
    """Prompt the user to install claude locally."""
    install_cmd = _build_install_command_hint(version)
    logger.info(
        "\nClaude is not installed on this machine.\nYou can install it by running:\n  {}\n",
        install_cmd,
    )
    return click.confirm("Would you like to install it now?", default=True)


def _warn_about_version_consistency(config: ClaudeAgentConfig, concurrency_group: ConcurrencyGroup) -> None:
    """Warn about potential version inconsistency when syncing local claude files to a remote host.

    When local claude files (settings, credentials) are synced to a remote host,
    version consistency matters:
    - If no version is pinned, the remote host may be running a different version
    - If a version is pinned but the local version differs, synced settings may be incompatible
    """
    local_version = _get_local_claude_version(concurrency_group)

    if config.version is None:
        logger.warning(
            "No claude version is pinned in agent config, but local claude files are being "
            "synced to the remote host. Consider setting 'version' in your claude agent config "
            "to ensure version consistency between local and remote. "
            "Local claude version: {}",
            local_version or "unknown",
        )
    elif local_version is not None and local_version != config.version:
        logger.warning(
            "Local claude version ({}) does not match the pinned version ({}). "
            "This may cause compatibility issues with synced settings.",
            local_version,
            config.version,
        )
    else:
        logger.debug("Version consistency check passed (pinned={}, local={})", config.version, local_version)


def _prompt_user_for_trust(source_path: Path) -> bool:
    """Prompt the user to trust a directory for Claude Code."""
    logger.info(
        "\nSource directory {} is not yet trusted by Claude Code.\n"
        "mngr needs to add a trust entry for this directory to ~/.claude.json\n"
        "so that the trust dialog doesn't interfere with automated input.\n",
        source_path,
    )
    return click.confirm("Would you like to update ~/.claude.json to trust this directory?", default=False)


def _prompt_user_for_effort_callout_dismissal() -> bool:
    """Prompt the user to dismiss the Claude Code effort callout."""
    logger.info(
        "\nClaude Code shows a one-time tip about setting model effort with /model.\n"
        "mngr needs to dismiss this tip in ~/.claude.json so that it doesn't\n"
        "interfere with automated input.\n",
    )
    return click.confirm("Would you like to update ~/.claude.json to dismiss this tip?", default=True)


def _prompt_user_for_onboarding_completion() -> bool:
    """Prompt the user to mark Claude Code onboarding as complete."""
    logger.info(
        "\nClaude Code onboarding has not been completed yet.\n"
        "mngr needs to mark onboarding as complete in ~/.claude.json so that\n"
        "the onboarding flow doesn't interfere with automated input.\n"
        "If you'd like to go through onboarding first, run `claude` directly.\n",
    )
    return click.confirm("Would you like to update ~/.claude.json to skip onboarding?", default=True)


def _claude_json_has_primary_api_key() -> bool:
    """Check if ~/.claude.json contains a non-empty primaryApiKey."""
    claude_json_path = find_user_config_in_isolated_mode()
    try:
        config_data = read_claude_config(claude_json_path)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read claude config at {}: {}", claude_json_path, e)
        return False
    return bool(config_data.get("primaryApiKey"))


def _read_macos_keychain_credential(label: str, concurrency_group: ConcurrencyGroup) -> str | None:
    """Read a credential from the macOS keychain by label."""
    try:
        result = concurrency_group.run_process_to_completion(
            ["security", "find-generic-password", "-l", label, "-w"],
            is_checked_after=False,
        )
    except ProcessSetupError:
        logger.debug("macOS security binary not found")
        return None
    if result.returncode != 0:
        logger.debug("No keychain credential found for label {!r}", label)
        return None
    return result.stdout.strip()


def _is_using_claude_oauth_subscription(source_claude_dir: Path, concurrency_group: ConcurrencyGroup) -> bool:
    """Detect whether the user authenticates Claude Code with subscription OAuth credentials.

    Claude Code stores claude.ai subscription tokens under the ``claudeAiOauth``
    key, either in ``<config_dir>/.credentials.json`` or -- on macOS -- in the
    login keychain under the "Claude Code-credentials" label. API-key auth uses a
    separate store ("Claude Code" / primaryApiKey), so the presence of OAuth
    credentials is a reliable signal that the user is on a subscription.
    """
    credentials_path = source_claude_dir / ".credentials.json"
    if credentials_path.exists():
        try:
            file_data = json.loads(credentials_path.read_text())
        except (json.JSONDecodeError, OSError):
            file_data = None
        if isinstance(file_data, dict) and "claudeAiOauth" in file_data:
            return True

    raw_keychain = _read_macos_keychain_credential("Claude Code-credentials", concurrency_group)
    if raw_keychain is None:
        return False
    try:
        keychain_data = json.loads(raw_keychain)
    except json.JSONDecodeError:
        return False
    return isinstance(keychain_data, dict) and "claudeAiOauth" in keychain_data


def _delete_macos_keychain_credential(label: str, concurrency_group: ConcurrencyGroup) -> bool:
    """Delete a credential from the macOS keychain by label.

    Returns True if the credential was deleted, False if it didn't exist or deletion failed.
    """
    account = getpass.getuser()
    try:
        result = concurrency_group.run_process_to_completion(
            ["security", "delete-generic-password", "-s", label, "-a", account],
            is_checked_after=False,
        )
    except ProcessSetupError:
        return False
    return result.returncode == 0


@pure
def _compute_keychain_label_suffix(config_dir: Path) -> str:
    """Compute the keychain label suffix Claude Code uses for a given CLAUDE_CONFIG_DIR.

    Claude Code appends -<sha256(config_dir)[:8]> to keychain labels when
    CLAUDE_CONFIG_DIR is set, to avoid collisions between config dirs.
    """
    normalized = str(config_dir).encode()
    return f"-{hashlib.sha256(normalized).hexdigest()[:8]}"


def _write_macos_keychain_credential(label: str, value: str, concurrency_group: ConcurrencyGroup) -> bool:
    """Write a credential to the macOS keychain under the given label.

    Returns True if the credential was written successfully.
    """
    account = getpass.getuser()
    # Remove any existing entry first -- add-generic-password fails if one already exists
    try:
        concurrency_group.run_process_to_completion(
            ["security", "delete-generic-password", "-s", label, "-a", account],
            is_checked_after=False,
        )
    except ProcessSetupError:
        pass
    try:
        result = concurrency_group.run_process_to_completion(
            ["security", "add-generic-password", "-s", label, "-a", account, "-l", label, "-w", value],
            is_checked_after=False,
        )
    except ProcessSetupError:
        logger.debug("macOS security binary not found")
        return False
    if result.returncode != 0:
        logger.warning("Failed to write keychain credential for label {!r}: {}", label, result.stderr)
        return False
    return True


def _provision_keychain_credentials(config_dir: Path, concurrency_group: ConcurrencyGroup) -> None:
    """macOS: copy keychain entries from the default label to the per-agent label.

    Claude Code hashes CLAUDE_CONFIG_DIR into keychain labels, so credentials
    stored under the default label are not found when CLAUDE_CONFIG_DIR is set.
    """
    suffix = _compute_keychain_label_suffix(config_dir)

    api_key = _read_macos_keychain_credential("Claude Code", concurrency_group)
    if api_key is not None:
        target = f"Claude Code{suffix}"
        if _write_macos_keychain_credential(target, api_key, concurrency_group):
            logger.debug("Copied API key to per-agent keychain label {!r}", target)

    credentials = _read_macos_keychain_credential("Claude Code-credentials", concurrency_group)
    if credentials is not None:
        target = f"Claude Code-credentials{suffix}"
        if _write_macos_keychain_credential(target, credentials, concurrency_group):
            logger.debug("Copied OAuth credentials to per-agent keychain label {!r}", target)


def _provision_local_credentials(host: OnlineHostInterface, config_dir: Path, *, symlink: bool) -> None:
    """Set up .credentials.json in the per-agent config dir (symlink or copy).

    When ``symlink`` is True, creates a symlink so credential updates propagate
    across sessions. When False, copies the file for full isolation.
    """
    credentials_source = get_user_claude_config_dir() / ".credentials.json"
    credentials_dest = config_dir / ".credentials.json"
    if credentials_source.exists():
        if symlink:
            host.execute_idempotent_command(
                f"ln -sfn {shlex.quote(str(credentials_source))} {shlex.quote(str(credentials_dest))}",
                timeout_seconds=5.0,
            )
        else:
            host.execute_idempotent_command(
                f"rm -f {shlex.quote(str(credentials_dest))}"
                f" && cp {shlex.quote(str(credentials_source))} {shlex.quote(str(credentials_dest))}"
                f" && chmod 600 {shlex.quote(str(credentials_dest))}",
                timeout_seconds=5.0,
            )
    else:
        logger.debug("No .credentials.json found to provision")


def _read_credentials_content(
    source_claude_dir: Path, config: ClaudeAgentConfig, concurrency_group: ConcurrencyGroup
) -> str | None:
    """Read credentials content from file or macOS keychain. Returns None if unavailable."""
    credentials_path = source_claude_dir / ".credentials.json"
    if credentials_path.exists():
        logger.info("Found .credentials.json at {}", credentials_path)
        return credentials_path.read_text()
    if config.convert_macos_credentials and is_macos():
        keychain_credentials = _read_macos_keychain_credential("Claude Code-credentials", concurrency_group)
        if keychain_credentials is not None:
            logger.info("Found macOS keychain OAuth credentials")
            return keychain_credentials
        logger.debug("No credentials found (file does not exist, no keychain credentials)")
    else:
        logger.debug("No credentials found (file does not exist at {})", credentials_path)
    return None


def _merge_keychain_api_key(
    claude_json_data: dict[str, Any],
    config: ClaudeAgentConfig,
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Inject primaryApiKey from the macOS keychain into claude_json_data if not already present."""
    if claude_json_data.get("primaryApiKey"):
        return
    if not config.convert_macos_credentials or not is_macos():
        return
    keychain_api_key = _read_macos_keychain_credential("Claude Code", concurrency_group)
    if keychain_api_key is None:
        return
    logger.info("Merging macOS keychain API key into per-agent .claude.json...")
    claude_json_data["primaryApiKey"] = keychain_api_key


def _write_generated_files(
    host: OnlineHostInterface,
    config_dir: Path,
    generated_files: dict[Path, str],
) -> None:
    """Write generated config files to the per-agent config dir.

    For local hosts, writes files directly. For remote hosts, stages
    files to a local temp dir and rsyncs them in a single call.
    """
    if host.is_local:
        for relative, content in generated_files.items():
            dest = config_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Break any existing symlink so we write a regular file instead
            # of following the symlink back to the source (e.g. ~/.claude/).
            # _sync_user_resources creates child-level symlinks for plugins/;
            # writing through them would corrupt the user's original files.
            if dest.is_symlink():
                dest.unlink()
            host.write_text_file(dest, content)
    else:
        # Remote host: transfer all generated files in a single rsync via the shared
        # bulk-upload helper (config_dir is absolute, so remote_home is unused).
        files: dict[Path, bytes | str | Path] = {
            config_dir / relative: content for relative, content in generated_files.items()
        }
        upload_files_in_bulk(host, files, "", skip_missing=False)


def _sync_user_resources(host: OnlineHostInterface, config_dir: Path, *, symlink: bool) -> None:
    """Sync user resource directories and files from the claude home dir into the per-agent config dir.

    Syncs directories (skills/, agents/, commands/, plugins/) and individual
    files (keybindings.json) depending on the ``symlink`` flag. In symlink mode,
    plugins/ and skills/ use child-level symlinks (not a dir-level symlink) so
    that per-agent real files can coexist with the shared source: plugins/ holds
    generated files (installed_plugins.json, known_marketplaces.json) and skills/
    holds a skill-provisioned agent's own primary skill, neither of which should
    leak back into the shared ~/.claude/. settings.json is handled separately by
    _build_settings_json. All symlinks use ``ln -sfn`` so that re-provisioning
    replaces an existing dest symlink instead of dereferencing it and nesting a
    new self-referential link inside the shared source.
    """
    home_claude = get_user_claude_config_dir()
    for dir_name in _CLAUDE_HOME_SYNC_DIRS:
        source = home_claude / dir_name
        if not source.exists():
            continue
        dest = config_dir / dir_name
        if not symlink:
            host.execute_idempotent_command(
                f"cp -r {shlex.quote(str(source))} {shlex.quote(str(dest))}", timeout_seconds=5.0
            )
        elif dir_name in _CLAUDE_HOME_CHILD_SYMLINK_DIRS:
            # Child-level symlinks so per-agent real files can coexist with shared
            # directory contents (cache/, marketplaces/, other skills, etc.). For
            # plugins/, skip the files that _write_generated_files overwrites;
            # symlinking them would cause writes to corrupt the shared source.
            host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(dest))}", timeout_seconds=5.0)
            skip_names = (
                {_INSTALLED_PLUGINS_RELATIVE_PATH.name, _KNOWN_MARKETPLACES_RELATIVE_PATH.name}
                if dir_name == "plugins"
                else set()
            )
            for child in source.iterdir():
                if child.name in skip_names:
                    continue
                host.execute_idempotent_command(
                    f"ln -sfn {shlex.quote(str(child))} {shlex.quote(str(dest / child.name))}",
                    timeout_seconds=5.0,
                )
        else:
            host.execute_idempotent_command(
                f"ln -sfn {shlex.quote(str(source))} {shlex.quote(str(dest))}", timeout_seconds=5.0
            )
    # Sync individual files (e.g. keybindings.json)
    for file_name in _CLAUDE_HOME_SYNC_FILES:
        source = home_claude / file_name
        if not source.exists():
            continue
        dest = config_dir / file_name
        if symlink:
            host.execute_idempotent_command(
                f"ln -sfn {shlex.quote(str(source))} {shlex.quote(str(dest))}", timeout_seconds=5.0
            )
        else:
            host.execute_idempotent_command(
                f"cp {shlex.quote(str(source))} {shlex.quote(str(dest))}", timeout_seconds=5.0
            )


def _rsync_claude_home_directories(
    host: OnlineHostInterface,
    local_claude_dir: Path,
    config_dir: Path,
) -> None:
    """Transfer directories and individual files from ~/.claude/ to a remote config dir using rsync.

    Uses a single host.copy_local_directory (rsync) call with include/exclude filters
    to transfer all directories (skills/, agents/, commands/, plugins/) and
    individual files (keybindings.json) at once. Generated files like
    settings.json are handled separately by the caller.
    """
    include_args: list[str] = []
    for dir_name in _CLAUDE_HOME_SYNC_DIRS:
        if not (local_claude_dir / dir_name).exists():
            continue
        include_args.extend([f"--include={dir_name}/", f"--include={dir_name}/**"])
    for file_name in _CLAUDE_HOME_SYNC_FILES:
        if not (local_claude_dir / file_name).exists():
            continue
        include_args.append(f"--include={file_name}")
    if not include_args:
        return
    include_args.append("--exclude=*")
    with log_span("Rsyncing claude home directories to per-agent config dir"):
        host.copy_local_directory(local_claude_dir, config_dir, " ".join(include_args))


def _resolve_plugins_dir_sentinel(host: OnlineHostInterface) -> None:
    """Resolve sentinel-prefixed paths in the claude home plugins directory.

    Deploy images have paths rewritten to a sentinel prefix at build time
    (because the container's home directory isn't known then). This resolves
    them to the actual claude home path in place, so all downstream
    provisioning code can assume paths use the real claude home as the prefix.

    Handles both installed_plugins.json (installPath) and
    known_marketplaces.json (installLocation).

    No-op if the files don't exist or don't contain the sentinel.
    """
    local_claude_dir = get_user_claude_config_dir()

    installed_plugins_path = local_claude_dir / _INSTALLED_PLUGINS_RELATIVE_PATH
    if installed_plugins_path.exists():
        content = installed_plugins_path.read_text()
        if _INSTALLED_PLUGINS_SENTINEL_PREFIX in content:
            rewritten = _rewrite_installed_plugins_paths(
                content, Path(_INSTALLED_PLUGINS_SENTINEL_PREFIX), local_claude_dir
            )
            installed_plugins_path.write_text(rewritten)

    known_marketplaces_path = local_claude_dir / _KNOWN_MARKETPLACES_RELATIVE_PATH
    if known_marketplaces_path.exists():
        content = known_marketplaces_path.read_text()
        if _INSTALLED_PLUGINS_SENTINEL_PREFIX in content:
            rewritten = _rewrite_known_marketplaces_paths(
                content, Path(_INSTALLED_PLUGINS_SENTINEL_PREFIX), local_claude_dir
            )
            known_marketplaces_path.write_text(rewritten)


def _load_claude_resource_script(filename: str) -> str:
    """Load a resource script from the mngr_claude resources package."""
    resource_files = importlib.resources.files(_claude_resources)
    script_path = resource_files.joinpath(filename)
    return script_path.read_text()


# The single common-transcript converter that is gated by
# emit_common_transcript (returned by ClaudeAgent.get_common_transcript_scripts).
# It is omitted from the agent's commands/ dir entirely when the user opts out.
_CLAUDE_COMMON_TRANSCRIPT_SCRIPT_NAME: Final[str] = "common_transcript.sh"

# The python converter that common_transcript.sh invokes (python3
# <dir>/common_transcript_convert.py). Provisioned alongside the .sh so the
# shell resolves it relative to itself; gated by the same emit_common_transcript.
_CLAUDE_COMMON_TRANSCRIPT_CONVERT_SCRIPT_NAME: Final[str] = "common_transcript_convert.py"

# The raw-transcript streamer (returned by ClaudeAgent.get_raw_transcript_scripts
# per HasTranscriptMixin). Always provisioned: it tails Claude's native session
# JSONL files into logs/claude_transcript/events.jsonl, which is read by the
# common transcript converter *and* by ClaudeAgent._build_accept_marker_command
# (the enqueue-marker fallback for the UserPromptSubmit-via-tmux-wait-for hook),
# so the streamer must keep running even when the common transcript is disabled.
_CLAUDE_RAW_TRANSCRIPT_SCRIPT_NAME: Final[str] = "stream_transcript.sh"

# Claude-specific scripts that are always provisioned regardless of
# emit_common_transcript:
#   - claude_background_tasks.sh is the long-running orchestrator launched
#     by assemble_command. It does activity tracking, supervises
#     stream_transcript.sh, and launches the common transcript converter
#     when it finds the script on disk -- so disabled-emit takes effect
#     simply by not provisioning common_transcript.sh.
#   - wait_for_stop_hook.sh and sync_keychain_credentials.py are unrelated
#     helpers invoked by Claude hooks.
#
# The raw-transcript streamer (stream_transcript.sh) is also always provisioned
# but is provisioned via :func:`provision_raw_transcript_scripts` because it
# satisfies the :class:`HasTranscriptMixin` contract.
_CLAUDE_ALWAYS_PROVISIONED_SCRIPT_NAMES: Final[tuple[str, ...]] = (
    "claude_background_tasks.sh",
    "wait_for_stop_hook.sh",
    "sync_keychain_credentials.py",
)

# The tmux-based response-streaming watcher. Provisioned only when
# streaming_snapshot_interval_seconds > 0; claude_background_tasks.sh launches it
# when it finds the script on disk (the presence check is the single gate, just
# like common_transcript.sh).
_CLAUDE_STREAM_SNAPSHOT_SCRIPT_NAME: Final[str] = "stream_snapshot.py"


def _provision_claude_always_on_scripts(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Write Claude's always-on background scripts to $MNGR_AGENT_STATE_DIR/commands/.

    The raw-transcript streamer is provisioned separately via
    :func:`provision_raw_transcript_scripts`, and the gated common-transcript
    converter via :func:`maybe_provision_common_transcript_scripts`.

    Note: mngr_log.sh (shared logging library) is provisioned by
    Host.provision_agent() to both host-level and agent-level commands
    directories, so we do not write it here.
    """
    scripts = {name: _load_claude_resource_script(name) for name in _CLAUDE_ALWAYS_PROVISIONED_SCRIPT_NAMES}
    provision_scripts_to_commands_dir(host, agent_state_dir, scripts, concurrency_group)


def _check_python3_available(host: OnlineHostInterface) -> None:
    """Raise PluginMngrError if python3 is not available on the host.

    The response-streaming watcher is a python script, so the host must have a
    python3 interpreter when streaming is enabled.
    """
    result = host.execute_idempotent_command("command -v python3", timeout_seconds=10.0)
    if not result.success:
        raise PluginMngrError(
            "streaming_snapshot_interval_seconds > 0 requires python3 on the agent host, "
            "but no python3 interpreter was found. Install python3 on the host or set "
            "streaming_snapshot_interval_seconds = 0 to disable response streaming."
        )


def _provision_stream_snapshot_script(
    host: OnlineHostInterface,
    agent_state_dir: Path,
    interval_seconds: float,
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Provision the response-streaming watcher script and its poll-interval file.

    The interval is written to a file (rather than passed via an env var) because
    env-var propagation into the background-tasks subshell that launches the
    watcher is unreliable; the watcher reads the interval from this file at
    runtime via $MNGR_AGENT_STATE_DIR, which it always has.
    """
    script = _load_claude_resource_script(_CLAUDE_STREAM_SNAPSHOT_SCRIPT_NAME)
    provision_scripts_to_commands_dir(
        host, agent_state_dir, {_CLAUDE_STREAM_SNAPSHOT_SCRIPT_NAME: script}, concurrency_group
    )
    interval_path = get_agent_claude_plugin_dir(agent_state_dir) / "stream_interval"
    host.write_file(interval_path, f"{interval_seconds}\n".encode(), "0644")


def _has_api_credentials_available(
    host: OnlineHostInterface,
    options: CreateAgentOptions,
    config: ClaudeAgentConfig,
    concurrency_group: ConcurrencyGroup,
) -> bool:
    """Check whether API credentials appear to be available for Claude Code.

    Checks environment variables (process env for local hosts, agent env vars,
    host env vars), local credentials file (~/.claude/.credentials.json), and
    primaryApiKey in ~/.claude.json.

    Returns True if any credential source is detected, False otherwise.
    """
    # Local hosts inherit the process environment via tmux
    if host.is_local and os.environ.get("ANTHROPIC_API_KEY"):
        return True

    for env_var in options.environment.env_vars:
        if env_var.key == "ANTHROPIC_API_KEY":
            return True

    if host.get_env_var("ANTHROPIC_API_KEY"):
        return True

    # Check credentials file or macOS keychain (OAuth tokens)
    credentials_path = get_user_claude_config_dir() / ".credentials.json"
    is_oauth_available = credentials_path.exists() or (
        config.convert_macos_credentials
        and is_macos()
        and _read_macos_keychain_credential("Claude Code-credentials", concurrency_group) is not None
    )
    if is_oauth_available:
        if host.is_local:
            return True
        if config.sync_claude_credentials:
            return True

    # Check primaryApiKey in ~/.claude.json or macOS keychain (API key)
    is_api_key_available = _claude_json_has_primary_api_key() or (
        config.convert_macos_credentials
        and is_macos()
        and _read_macos_keychain_credential("Claude Code", concurrency_group) is not None
    )
    if is_api_key_available:
        if host.is_local:
            return True
        if config.sync_claude_json:
            return True

    return False


class DialogIndicator(FrozenModel, ABC):
    """Base class for dialog indicators that can block agent input."""

    @abstractmethod
    def get_match_string(self) -> str:
        """Return the primary string to look for in the tmux pane content."""
        ...

    @abstractmethod
    def get_description(self) -> str:
        """Return a human-readable description for error messages."""
        ...

    def matches(self, content: str) -> bool:
        """Check whether this dialog is present in the given pane content.

        Default implementation checks for get_match_string() in the content.
        Subclasses can override for more complex matching (e.g. multiple strings).
        """
        return self.get_match_string() in content


class DialogDetectedError(SendMessageError):
    """A dialog is blocking the agent's input in the terminal."""

    def __init__(self, agent_name: str, dialog_description: str) -> None:
        self.dialog_description = dialog_description
        super().__init__(
            agent_name,
            f"A dialog is blocking the agent's input ({dialog_description} detected in terminal). "
            f"Connect to the agent with 'mngr connect {agent_name}' to resolve it.",
        )


class TrustDialogIndicator(DialogIndicator):
    """Detects the Claude Code workspace trust dialog shown on first launch in a directory."""

    def get_match_string(self) -> str:
        return "Yes, I trust this folder"

    def get_description(self) -> str:
        return "trust dialog"


class CustomApiKeyDialogIndicator(DialogIndicator):
    """Detects the Claude Code dialog asking about whether to use an API defined in an env var."""

    def get_match_string(self) -> str:
        return "Detected a custom API key in your environment"

    def get_description(self) -> str:
        return "API key dialog"


class ThemeSelectionIndicator(DialogIndicator):
    """Detects the Claude Code theme selection prompt shown during onboarding."""

    def get_match_string(self) -> str:
        return "Choose the text style that looks best with your terminal"

    def get_description(self) -> str:
        return "theme selection dialog"


class EffortCalloutIndicator(DialogIndicator):
    """Detects the Claude Code effort callout shown after model selection."""

    def get_match_string(self) -> str:
        return "You can always change effort in /model later."

    def get_description(self) -> str:
        return "effort callout"


class CostThresholdDialogIndicator(DialogIndicator):
    """Detects the Claude Code cost threshold dialog shown when API spending reaches a threshold.

    This dialog blocks all input and must be acknowledged. It is detected by the
    presence of both the spending guidance text and the claude code docs URL.
    """

    _MATCH_SPENDING_TEXT: str = "Learn more about how to monitor your spending:"
    _MATCH_DOCS_URL: str = "https://code.claude.com/"

    def get_match_string(self) -> str:
        return self._MATCH_SPENDING_TEXT

    def get_description(self) -> str:
        return "cost threshold dialog"

    def matches(self, content: str) -> bool:
        """Check for both the spending text and the docs URL in the pane content."""
        return self._MATCH_SPENDING_TEXT in content and self._MATCH_DOCS_URL in content


class ClaudeCoreAgent(
    BaseAgent[ClaudeAgentConfig],
    CliBackedAgentMixin,
    HasCommonTranscriptMixin,
    HasSessionPreservationMixin,
    HasUnattendedModeMixin,
    HasVersionManagementMixin,
    HasAutoInstallMixin,
):
    """Shared core for Claude agents (interactive and headless).

    Holds everything not tied to the interactive TUI: config-dir setup,
    credentials, transcript scripts, session preservation, auto-install, version
    management, and the provisioning flow. The interactive :class:`ClaudeAgent`
    subclass adds the TUI send/readiness pipeline, the streaming snapshot, and
    session adoption; the headless variant inherits this core directly and so
    does not carry those interactive-only capabilities.
    """

    @property
    def is_common_transcript_enabled(self) -> bool:
        return self.agent_config.emit_common_transcript

    def get_raw_transcript_scripts(self) -> Mapping[str, str]:
        """Return Claude's raw-transcript streamer script.

        Always provisioned (per :class:`HasTranscriptMixin`): the streamer
        tails Claude's native session JSONL into
        ``logs/claude_transcript/events.jsonl``, which feeds both the
        common-transcript converter and the enqueue-marker fallback in
        ``_build_accept_marker_command``. The background orchestrator that
        supervises this streamer is provisioned separately by
        ``_provision_claude_always_on_scripts``.
        """
        return {_CLAUDE_RAW_TRANSCRIPT_SCRIPT_NAME: _load_claude_resource_script(_CLAUDE_RAW_TRANSCRIPT_SCRIPT_NAME)}

    def get_common_transcript_scripts(self) -> Mapping[str, str]:
        """Return only the script gated by ``emit_common_transcript``.

        For Claude that's a converter shell script (``common_transcript.sh``)
        plus the python module it invokes (``common_transcript_convert.py``).
        The raw transcript streamer is on
        :meth:`get_raw_transcript_scripts` and the background
        orchestrator that supervises it is in
        ``_provision_claude_always_on_scripts``; both run regardless of
        whether the common transcript is on.
        """
        return {
            name: _load_claude_resource_script(name)
            for name in (
                _CLAUDE_COMMON_TRANSCRIPT_SCRIPT_NAME,
                _CLAUDE_COMMON_TRANSCRIPT_CONVERT_SCRIPT_NAME,
            )
        }

    def get_claude_config_dir(self) -> Path:
        """Return the Claude config directory for this agent.

        Default: per-agent isolated directory at
        ``$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/`` that replaces
        ``~/.claude/`` for this agent.

        In shared mode (local host + ``isolate_local_config_dir=False``):
        resolve to the value of ``$CLAUDE_CONFIG_DIR`` (the user's shared config
        dir), so multiple agents share a single directory. When the env var is
        unset, falls back to ``~/.claude/`` so the agent uses claude's own
        default.
        """
        if self._is_isolated_config_dir():
            return get_agent_claude_config_dir(self._get_agent_dir())
        return resolve_shared_claude_config_dir()

    def _is_isolated_config_dir(self) -> bool:
        """Whether this agent uses a per-agent (isolated) Claude config dir.

        ``isolate_local_config_dir`` only governs LOCAL agents. Remote agents
        always use an isolated per-agent config dir regardless of the flag: the
        user's ``$CLAUDE_CONFIG_DIR`` / keychain live on the local machine and
        are not reachable on the remote host, so there is nothing to share. This
        is why the flag carries ``local`` in its name -- it is ignored when the
        host is not local. ``resolve_isolate_local_config_dir`` reconciles the
        deprecated ``use_env_config_dir`` alias.
        """
        return self.agent_config.resolve_isolate_local_config_dir() or not self.host.is_local

    def _dialog_dismissal_config_path(self) -> Path:
        """Return the global ``.claude.json`` whose startup-dialog state this agent dismisses.

        Both modes record dialog dismissals (trust, onboarding, effort callout, cost
        threshold) in the user's *global* config -- these are cosmetic first-run prompts,
        never tool-permission grants (``bypassPermissionsModeAccepted`` is deliberately
        left untouched; see ``auto_dismiss_claude_dialogs``).

        Isolated mode writes to ``find_user_config_in_isolated_mode`` (the per-agent config dir is
        built from it via ``sync_local``, so it inherits the dismissals). Shared mode
        writes to ``find_user_config_in_unisolated_mode``, the file the agent's claude reads
        directly, resolved the same way ``modify_env_vars`` resolves ``CLAUDE_CONFIG_DIR``.
        """
        if self._is_isolated_config_dir():
            return find_user_config_in_isolated_mode()
        return find_user_config_in_unisolated_mode()

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Add CLAUDE_CONFIG_DIR and ORIGINAL_CLAUDE_CONFIG_DIR.

        In isolated mode CLAUDE_CONFIG_DIR points at the per-agent config dir.

        In shared mode (``isolate_local_config_dir=False``) we export
        CLAUDE_CONFIG_DIR *only* when the user's own shell already had it set.
        Exporting it unconditionally is NOT a no-op even when the value equals the
        ``~/.claude`` that claude defaults to: claude reads its global
        ``.claude.json`` (onboarding state, theme, trust, history, ...) from
        ``$CLAUDE_CONFIG_DIR/.claude.json`` when the var is set, but from
        ``~/.claude.json`` (beside the dir) when it is unset. Forcing
        ``CLAUDE_CONFIG_DIR=~/.claude`` therefore points claude at an inner stub
        file that lacks the user's onboarding state, re-triggering the
        theme/onboarding screen on every shared-mode agent. Leaving it unset
        preserves claude's own default resolution. The launch command in
        ``assemble_command`` no longer depends on the var being exported: its
        session-file lookup falls back to ``$HOME/.claude`` via
        ``${CLAUDE_CONFIG_DIR:-$HOME/.claude}``.

        In shared mode we also do not set ORIGINAL_CLAUDE_CONFIG_DIR (there is no
        per-agent dir to distinguish from the user's) and do not force
        DISABLE_AUTOUPDATER, leaving the user's claude environment otherwise
        alone.

        The common-transcript opt-in/out is gated at provisioning time -- when
        disabled, the converter script is not written to commands/, so the
        background orchestrator finds nothing to launch.

        When the resolved update policy is NEVER, sets DISABLE_AUTOUPDATER=1 so
        Claude Code's background auto-updater does not move the binary off its
        installed (possibly pinned) version. setdefault leaves an explicit
        user-provided value untouched.
        """
        config = self.agent_config
        if self._is_isolated_config_dir():
            env_vars["CLAUDE_CONFIG_DIR"] = str(self.get_claude_config_dir())
            env_vars["ORIGINAL_CLAUDE_CONFIG_DIR"] = str(get_user_claude_config_dir())
            if is_self_update_disabled(config.update_policy, is_unattended=not host.is_local):
                env_vars.setdefault("DISABLE_AUTOUPDATER", "1")
        else:
            # Shared mode: only propagate CLAUDE_CONFIG_DIR when the user's own
            # shell already exported it (in which case their .claude.json already
            # lives inside that dir and sharing stays consistent). When unset,
            # leave it unset so claude resolves its default ~/.claude.json rather
            # than an empty inner stub. See the docstring for why this matters.
            user_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
            if user_config_dir:
                env_vars["CLAUDE_CONFIG_DIR"] = user_config_dir

    def get_lifecycle_state(self) -> AgentLifecycleState:
        """Get lifecycle state, accounting for Claude-specific permissions_waiting file.

        The PermissionRequest hook creates a 'permissions_waiting' file when Claude
        is blocked on a permission dialog. When present, this overrides RUNNING to
        WAITING since the agent cannot make progress without user intervention.

        Delegates the gating decision to the shared classify_waiting_reason so this
        promotion and the waiting_reason field generator cannot drift: a RUNNING
        base state means the 'active' marker is present and the process is alive, so
        the classifier's is_active gate is satisfied and a PERMISSIONS verdict is
        what promotes RUNNING to WAITING.
        """
        state = super().get_lifecycle_state()
        if state != AgentLifecycleState.RUNNING:
            return state
        is_blocked = self._check_file_exists(self._get_agent_dir() / "permissions_waiting")
        reason = classify_waiting_reason(is_active=True, is_blocked_on_permission=is_blocked)
        return AgentLifecycleState.WAITING if reason is WaitingReason.PERMISSIONS else state

    def get_expected_process_name(self) -> str:
        """Return 'claude' as the expected process name.

        This overrides the base implementation because ClaudeAgent uses a complex
        shell command with exports and fallbacks, but the actual process is always 'claude'.
        """
        return "claude"

    def get_provision_file_transfers(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> Sequence[FileTransferSpec]:
        """Return file transfers for claude settings."""
        config = self.agent_config
        transfers: list[FileTransferSpec] = []

        # Transfer repo-local claude settings
        if config.sync_repo_settings:
            claude_dir = self.work_dir / ".claude"
            for file_path in claude_dir.rglob("*.local.*"):
                relative_path = file_path.relative_to(self.work_dir)
                transfers.append(
                    FileTransferSpec(local_path=file_path, agent_path=RelativePath(relative_path), is_required=True)
                )

        # Transfer override folder contents
        if config.override_settings_folder is not None:
            override_folder = config.override_settings_folder
            if override_folder.is_dir():
                for file_path in override_folder.rglob("*"):
                    if file_path.is_file():
                        relative_path = file_path.relative_to(override_folder)
                        remote_path = Path(".claude") / relative_path
                        transfers.append(
                            FileTransferSpec(
                                local_path=file_path,
                                agent_path=RelativePath(remote_path),
                                is_required=False,
                            )
                        )

        return transfers

    def _configure_agent_hooks(self, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
        """Write mngr's hooks (and the user's settings_overrides) to the managed settings file.

        This is the ``use_env_config_dir``-mode channel only. In that mode there
        is no per-agent config dir to bake hooks into, so mngr loads them at
        launch via ``claude --settings`` (see ``assemble_command``) from a file
        it owns under the agent state dir -- not the project's
        ``.claude/settings.local.json``, which plain ``claude`` also reads (see
        ``get_managed_settings_path``). Overwritten fresh each provision, so it
        never accumulates stale hooks.

        In normal mode the same content is baked into the per-agent config-dir
        ``settings.json`` by ``_build_settings_json`` instead; this method is not
        called.

        Always writes the readiness hooks (which mark the agent active/idle via
        files in its state dir). Adds the macOS keychain-sync hook when
        sync_credentials_on_login is set, and the permission auto-allow hook when
        unattended. Then folds the user's ``settings_overrides`` patch onto that,
        so the managed file is the single ``--settings`` overlay Claude layers
        (highest precedence) over the user's shared config -- the base for the
        fold is mngr's hooks (not the shared config, which Claude layers itself),
        so narrowing here only guards against an override dropping mngr's hooks.
        """
        # The always-on readiness hooks, plus the optional credential-sync (macOS) and
        # permission auto-allow hooks, with the user's settings_overrides folded on top.
        settings = fold_hook_configs(
            {}, build_mngr_hook_configs(self.agent_config, is_unattended=self.is_unattended_enabled())
        )
        settings = apply_settings_patch(
            settings,
            self.agent_config.settings_overrides,
            allow_narrowing=mngr_ctx.config.allow_settings_key_assignment_narrowing,
            base_description="mngr's managed Claude hooks",
        )

        settings_path = get_managed_settings_path(self._get_agent_dir())
        # The plugin/claude/ parent may not exist yet (in use_env_config_dir
        # mode the per-agent config dir is not provisioned), so create it.
        with log_span("Configuring agent hooks in {}", settings_path):
            write_json_dict_via_host(host, settings_path, settings, make_parent=True)

    def _dismiss_start_dialogs(
        self, host: OnlineHostInterface, options: CreateAgentOptions, mngr_ctx: MngrContext
    ) -> None:
        """No-op for core/headless claude: there is no interactive TUI whose startup
        dialogs could intercept input, so no dialog handling runs at all. The
        interactive :class:`ClaudeAgent` subclass overrides this to dismiss (or
        auto-dismiss) trust / onboarding / effort dialogs before the agent starts.
        """

    def _find_git_source_path(self, concurrency_group: ConcurrencyGroup) -> Path | None:
        """Find the source repo path for the agent's work_dir, if it's a git worktree or mirror.

        Returns the parent of the git common dir (the source repo root),
        or None if work_dir is not inside a git repo. Delegates to the shared
        core helper ``imbue.mngr.utils.git_utils.find_git_source_path`` (also
        used by ``mngr_antigravity``).
        """
        return find_git_source_path(self.work_dir, concurrency_group)

    def _setup_per_agent_config_dir(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Create and populate the per-agent Claude config directory.

        Unified flow for local and remote hosts:
        1. Build runtime context (ProvisioningContext)
        2. Generate all file contents (.claude.json, settings.json, installed_plugins.json)
        3. Transfer directories (symlink/rsync) and set up credentials
        4. Stage generated files to temp dir and copy to config_dir
        """
        config = self.agent_config
        config_dir = self.get_claude_config_dir()
        source_claude_dir = get_user_claude_config_dir()
        logger.debug(
            "_setup_per_agent_config_dir: agent={} host.is_local={} config_dir={} "
            "sync_home_settings={} sync_claude_json={} sync_claude_credentials={}",
            self.id,
            host.is_local,
            config_dir,
            config.sync_home_settings,
            config.sync_claude_json,
            config.sync_claude_credentials,
        )

        # Build runtime context
        copy_project_config_from: Path | None = None
        if host.is_local and options.transfer_mode in (TransferMode.GIT_WORKTREE, TransferMode.GIT_MIRROR):
            copy_project_config_from = self._find_git_source_path(mngr_ctx.concurrency_group)
        ctx = ProvisioningContext(
            is_unattended=not host.is_local,
            is_auto_approve=mngr_ctx.is_auto_approve,
            copy_project_config_from=copy_project_config_from,
        )

        # Create the config directory (0700: contains credentials and session data)
        host.execute_idempotent_command(f"mkdir -p -m 0700 {shlex.quote(str(config_dir))}", timeout_seconds=5.0)

        # Warn about version consistency when syncing local files to remote
        if not host.is_local and (
            config.sync_home_settings or config.sync_claude_json or config.sync_claude_credentials
        ):
            _warn_about_version_consistency(config, mngr_ctx.concurrency_group)

        # Resolve work_dir on remote hosts (e.g. Modal symlinks /mngr/ -> /__modal/volumes/)
        work_dir = self.work_dir
        if not host.is_local:
            realpath_result = host.execute_idempotent_command(
                f"realpath {shlex.quote(str(self.work_dir))}", timeout_seconds=5.0
            )
            if realpath_result.success and realpath_result.stdout.strip():
                work_dir = Path(realpath_result.stdout.strip())

        # 1. Generate all file contents
        claude_json_data = _build_claude_json(
            work_dir=work_dir,
            config=config,
            ctx=ctx,
            sync_local=config.sync_claude_json,
            version=config.version,
        )
        # Pass host + options so approval finds keys arriving via --env, --pass-env,
        # --pass-host-env, --host-env, and --host-env-file -- not just os.environ. The
        # LOCAL/Docker minds path lands its ANTHROPIC_API_KEY only on the host's env
        # file (via --host-env-file <repo>/.env), so without these arguments the
        # approval missed the key and claude blocked on the custom-key TUI prompt.
        approve_api_key_for_claude(claude_json_data, host=host, options=options)

        settings_json = _build_settings_json(
            source_claude_dir,
            config,
            ctx,
            sync_local=config.sync_home_settings,
            is_unattended=self.is_unattended_enabled(),
            allow_narrowing=mngr_ctx.config.allow_settings_key_assignment_narrowing,
        )

        generated_files: dict[Path, str] = {
            Path("settings.json"): settings_json,
            Path(".claude.json"): json.dumps(claude_json_data, indent=2) + "\n",
        }
        if config.sync_home_settings and not host.is_local:
            # Rewrite plugin paths for remote hosts where ~/.claude/ doesn't exist.
            # Local hosts don't need rewriting: the original absolute paths under
            # ~/.claude/ are directly accessible, and _sync_user_resources already
            # provides the file (via symlink or copy).
            installed_plugins = _generate_installed_plugins_content(source_claude_dir, config_dir)
            if installed_plugins:
                generated_files[_INSTALLED_PLUGINS_RELATIVE_PATH] = installed_plugins
        if config.sync_home_settings:
            # Rewrite marketplace installLocation for both local and remote hosts.
            # Claude Code expects installLocation to point inside $CLAUDE_CONFIG_DIR.
            # Without rewriting, the paths point to ~/.claude/plugins/marketplaces/
            # which Claude Code treats as "corrupted", silently skipping marketplace
            # refreshes and leaving the plugin cache stale.
            known_marketplaces = _generate_known_marketplaces_content(source_claude_dir, config_dir)
            if known_marketplaces:
                generated_files[_KNOWN_MARKETPLACES_RELATIVE_PATH] = known_marketplaces

        # Remote credentials: read locally, include in generated files for staging
        if not host.is_local and config.sync_claude_credentials:
            credentials = _read_credentials_content(source_claude_dir, config, mngr_ctx.concurrency_group)
            if credentials:
                generated_files[Path(".credentials.json")] = credentials

        # Remote API key: merge from keychain if not already in .claude.json
        if not host.is_local:
            _merge_keychain_api_key(claude_json_data, config, mngr_ctx.concurrency_group)
            # Re-serialize after potential keychain merge
            generated_files[Path(".claude.json")] = json.dumps(claude_json_data, indent=2) + "\n"

        # 2. Transfer directories and set up local credentials
        if config.sync_home_settings:
            if host.is_local:
                _sync_user_resources(host, config_dir, symlink=config.symlink_user_resources)
            else:
                _rsync_claude_home_directories(host, source_claude_dir, config_dir)
        if host.is_local:
            if config.convert_macos_credentials and is_macos():
                _provision_keychain_credentials(config_dir, mngr_ctx.concurrency_group)
            else:
                _provision_local_credentials(host, config_dir, symlink=config.sync_credentials_on_login)

        # 3. Write generated files to config_dir
        _write_generated_files(host, config_dir, generated_files)

    def _maybe_warn_subscription_credentials(self, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
        """Warn macOS subscription users that config-dir isolation breaks credentials.

        Claude Code hashes ``CLAUDE_CONFIG_DIR`` into the macOS keychain label, so
        an isolated (per-agent) config dir gets its own credential entry. mngr
        seeds a copy of the user's OAuth tokens there at provision time, but
        claude.ai subscription tokens are refreshed periodically and the per-agent
        copy goes stale, so the agent eventually fails to authenticate. Sharing
        the user's config dir (``isolate_local_config_dir=False``) avoids this by
        reusing the same keychain entry as the user's own claude. This is only a
        warning -- the user may have reasons to keep isolation on -- and prints
        the exact command to turn it off.
        """
        config = self.agent_config
        if not (host.is_local and is_macos() and config.resolve_isolate_local_config_dir()):
            return
        if not _is_using_claude_oauth_subscription(get_user_claude_config_dir(), mngr_ctx.concurrency_group):
            return
        logger.warning(
            "Detected Claude.ai subscription (OAuth) credentials on macOS while Claude config-dir "
            "isolation is enabled. Isolated agents use a separate keychain entry whose copy of your "
            "subscription credentials goes stale as the tokens refresh, so this agent will likely hit "
            "authentication errors later. To share your default Claude config (and credentials) instead, run:\n"
            "    mngr config set agent_types.claude.isolate_local_config_dir false --scope user"
        )

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Provision the per-agent config dir, install Claude, and configure hooks.

        For local hosts, ensures all known Claude startup dialogs are dismissed
        in the global config so they don't intercept tmux input. Trust handling
        depends on the transfer mode:
        - git-worktree/git-mirror: trust is extended from the source directory
        - rsync/none: trust is prompted for the work_dir
        - auto_dismiss_dialogs=True: trust is auto-added for work_dir

        In shared mode (``isolate_local_config_dir=False``): mngr still dismisses
        the cosmetic startup dialogs (trust, onboarding, effort callout, cost
        threshold) directly in the user's global config so they don't intercept
        tmux input -- writing there is the whole point of shared mode, and these
        are not tool-permission grants. It still skips per-agent-only work: plugin
        path sentinel resolution and per-agent config dir setup.
        """
        config = self.agent_config

        # macOS subscription users hit credential trouble with per-agent config
        # isolation (the per-agent keychain copy of their OAuth tokens goes stale
        # as the subscription refreshes them). Warn before doing any work, with
        # the exact command to switch to the shared config dir.
        self._maybe_warn_subscription_credentials(host, mngr_ctx)

        # Resolve sentinel-prefixed installPaths in ~/.claude/ if present.
        # Deploy images have paths rewritten to a sentinel at build time
        # (because the container's home dir isn't known at build). Resolve
        # them to the actual ~/.claude/ path now, so all downstream code
        # can assume paths use ~/.claude/ as the prefix. Skipped in shared
        # mode because we don't want to rewrite the user's persistent config.
        if self._is_isolated_config_dir():
            _resolve_plugins_dir_sentinel(host)

        with mngr_ctx.concurrency_group.make_concurrency_group("claude_provisioning") as concurrency_group:
            # Provision Claude's always-on background scripts (activity
            # tracker, hook helpers), the always-on raw-transcript streamer
            # (per HasTranscriptMixin), and -- when the user has not opted
            # out -- the gated common-transcript converter. Splitting the
            # three paths is what makes emit_common_transcript=False
            # actually take effect on disk: claude_background_tasks.sh only
            # launches the converter if it finds it in commands/, and we
            # don't write it there if the flag is off.
            provision_backgroun_script_thread = concurrency_group.start_new_thread(
                _provision_claude_always_on_scripts,
                (host, self._get_agent_dir(), concurrency_group),
            )
            provision_raw_transcript_scripts(self, host, self._get_agent_dir(), concurrency_group)
            maybe_provision_common_transcript_scripts(self, host, self._get_agent_dir(), concurrency_group)

            # Provision the response-streaming watcher only when enabled. Its
            # presence on disk is what makes claude_background_tasks.sh launch
            # it, so a disabled interval simply means the script is absent.
            if config.streaming_snapshot_interval_seconds > 0:
                _check_python3_available(host)
                _provision_stream_snapshot_script(
                    host,
                    self._get_agent_dir(),
                    config.streaming_snapshot_interval_seconds,
                    concurrency_group,
                )

            # Dismiss start dialogs (TUI-only; a no-op on the headless core).
            self._dismiss_start_dialogs(host, options, mngr_ctx)

            # Ensure claude is installed via the shared helper (consent-gated locally,
            # config-gated remotely; claude's get_install_command pins the version),
            # then reconcile the present binary's version against any pin.
            if config.check_installation:
                ensure_cli_installed(host, mngr_ctx, self.get_install_binary_name(), self.get_install_command())
                self.reconcile_installed_version(host, mngr_ctx)

            # no matter what, *always* dismiss the cost popup, it's pointless. In
            # shared mode this targets the user's global config (the file claude
            # actually reads); in isolated mode the per-agent config inherits it.
            acknowledge_cost_threshold(self._dialog_dismissal_config_path())

            # Transfer plugin data from source agent before config setup (if cloning via --from).
            # This copies sessions, memory, transcript offsets, etc. The subsequent config setup
            # will overwrite identity-specific files (.claude.json, credentials) with fresh values.
            if options.source_agent_state_location is not None:
                self._transfer_source_plugin_data(options.source_agent_state_location)

            # Set up per-agent config directory (skipped in shared mode -- the
            # shared $CLAUDE_CONFIG_DIR is the user's responsibility to populate).
            if self._is_isolated_config_dir():
                self._setup_per_agent_config_dir(host, options, mngr_ctx)

            # Configure mngr's hooks. In isolated mode they are baked into the
            # per-agent config-dir settings.json by _setup_per_agent_config_dir
            # (-> _build_settings_json) above. In shared mode there is no per-agent
            # config dir, so write the managed --settings file instead. Keyed off the
            # resolved predicate (not the deprecated use_env_config_dir alias) so it
            # also fires when shared mode is set via isolate_local_config_dir=False.
            if not self._is_isolated_config_dir():
                self._configure_agent_hooks(host, mngr_ctx)

            # should be done by now, just wanted to do in parallel for latency reasons
            provision_backgroun_script_thread.join(60.0)

    def _transfer_source_plugin_data(self, source_agent_state_location: HostLocation) -> None:
        """Rsync the source agent's ``plugin/`` into this agent's state dir.
        Runs before ``_setup_per_agent_config_dir`` (which overwrites
        identity-specific files); the destination-side rewiring runs later
        in ``on_after_provisioning`` via ``_adopt_cloned_session``.
        """
        source_host = source_agent_state_location.host
        source_plugin_dir = source_agent_state_location.path / "plugin"
        dest_plugin_dir = self._get_agent_dir() / "plugin"

        if not source_host.path_exists(source_plugin_dir):
            logger.debug("No plugin directory in source agent, skipping clone transfer")
            return

        with log_span("Transferring source plugin data"):
            self.host.copy_directory(source_host, source_plugin_dir, dest_plugin_dir)

    def _resolve_work_dir_on_host(self) -> Path:
        """Return ``self.work_dir`` with symlinks resolved as the destination
        host sees it. On Modal, ``/mngr/projects/agent-<uuid>`` is a symlink
        onto ``/__modal/volumes/<vol-id>/projects/agent-<uuid>``; claude
        uses the resolved form for its cwd and per-project storage.

        Falls back to the unresolved path on ``readlink -f`` failure, but
        warns -- on a host where the canonical path differs, the fallback
        will silently break clone-resume.
        """
        result = self.host.execute_idempotent_command(
            f"readlink -f {shlex.quote(str(self.work_dir))}", timeout_seconds=5.0
        )
        if result.success and result.stdout.strip():
            return Path(result.stdout.strip())
        logger.warning(
            "readlink -f {} failed (success={}, stderr={!r}); falling back to unresolved path",
            self.work_dir,
            result.success,
            result.stderr.strip(),
        )
        return self.work_dir

    def preserve_session_state(self, host: OnlineHostInterface) -> None:
        preserve_agent_state(_claude_preserved_items(is_shared_config=not self._is_isolated_config_dir()), self, host)

    def is_unattended_enabled(self) -> bool:
        return self.agent_config.auto_allow_permissions

    def reconcile_installed_version(self, host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
        """Verify the installed claude matches the pinned version, if one is set.

        claude pins a specific version when ``config.version`` is set, otherwise it
        follows its own auto-update (nothing to enforce). With a pin, the installed
        binary must match -- a mismatch means the wrong version is on PATH, which the
        user must resolve (re-install or update the pin).
        """
        pinned_version = self.agent_config.version
        if pinned_version is None:
            return
        installed_version = _get_claude_version(host)
        if installed_version != pinned_version:
            raise AgentInstallationError(
                f"Claude version mismatch: installed version is {installed_version!r}, "
                f"but agent config pins version {pinned_version!r}. "
                "Re-install claude with the correct version or update the pinned version in your agent config."
            )
        logger.debug("Claude version {} matches pinned version", installed_version)

    def get_install_binary_name(self) -> str:
        return "claude"

    def get_install_command(self) -> str:
        return _build_claude_install_command(self.agent_config.version)

    def on_destroy(self, host: OnlineHostInterface) -> None:
        """Preserve session files and clean up per-agent credentials and trust entries.

        When preserve_sessions_on_destroy is enabled (default), copies session JSONL
        files, transcripts, and session history to the local mngr data directory
        before the agent state directory is deleted. For remote agents, files are
        pulled to the local machine so they survive host destruction.

        For agents with per-agent config dirs: cleans up macOS keychain entries
        (the config dir itself is deleted with the agent state).
        For legacy agents without per-agent config dirs: cleans up the global
        ~/.claude.json trust entry.

        In shared mode (``isolate_local_config_dir=False``): skip keychain / trust
        cleanup entirely.
        ``get_claude_config_dir()`` resolves to the user's shared $CLAUDE_CONFIG_DIR,
        which exists, so the per-agent-keychain branch would otherwise compute the
        same label hash Claude Code itself uses and delete the user's real
        credentials. provision() does write dialog-dismissal markers (trust,
        onboarding, effort, cost) into the user's global config in this mode, but we
        deliberately leave them: they are merged into the user's own global state
        (reverting onboarding/effort would harm the user), and the per-agent
        isolated path likewise leaves its global trust markers behind. Session
        preservation also skips copying the ``projects/`` directory in this mode
        (it lives in the user's persistent dir and contains all of their
        cross-project session history); only transcripts and the session-id
        history from the agent state dir are preserved.
        """
        # Preserve session files before the state dir is deleted
        if self.agent_config.preserve_sessions_on_destroy:
            self.preserve_session_state(host)

        if not self._is_isolated_config_dir():
            # Shared-config mode: mngr never wrote per-agent keychain entries, and the
            # dialog markers it did write to the global config are intentionally left
            # in place (see docstring). Any keychain delete here would target the
            # user's own credentials.
            return

        config_dir = self.get_claude_config_dir()
        per_agent_config_exists = host.execute_idempotent_command(
            f"test -d {shlex.quote(str(config_dir))}", timeout_seconds=5.0
        ).success

        if per_agent_config_exists and is_macos():
            # Clean up per-agent keychain entries
            suffix = _compute_keychain_label_suffix(config_dir)
            cg = self.mngr_ctx.concurrency_group
            if _delete_macos_keychain_credential(f"Claude Code{suffix}", cg):
                logger.debug("Removed per-agent API key keychain entry")
            if _delete_macos_keychain_credential(f"Claude Code-credentials{suffix}", cg):
                logger.debug("Removed per-agent OAuth credentials keychain entry")
        elif not per_agent_config_exists:
            # Legacy agent without per-agent config dir -- clean up global file
            removed = remove_claude_trust_for_path(find_user_config_in_isolated_mode(), self.work_dir)
            if removed:
                logger.debug("Removed Claude trust entry for {} from global config", self.work_dir)
        else:
            # Per-agent config dir on non-macOS: config dir is deleted with agent state, nothing extra to clean up
            pass


class ClaudeAgent(
    ClaudeCoreAgent,
    InteractiveTuiAgent[ClaudeAgentConfig],
    SupportsLiveOutputMixin,
    HasSessionAdoptionMixin,
):
    """Interactive (TUI-driven) Claude agent.

    Adds, on top of :class:`ClaudeCoreAgent`, the keystroke send / readiness
    pipeline, the live streaming snapshot, and session adoption
    (``--adopt`` / ``--from`` carry-forward). The headless variant
    inherits the core directly and so carries none of these interactive-only
    capabilities.
    """

    # The input-prompt glyph rendered by Claude Code's prompt box. Unlike the
    # "Claude Code" welcome banner, it appears on BOTH a fresh start and a
    # resume (the welcome banner is absent when resuming a saved session) and
    # stays visible while a turn is processing, making it a universal readiness
    # signal for every send path.
    TUI_READY_INDICATOR = "❯"

    # Path template for the transcript event log that the acceptance-marker
    # probe (see _build_accept_marker_command) reads as the fallback source when
    # the UserPromptSubmit hook misfires. The embedded $MNGR_AGENT_STATE_DIR is
    # evaluated on the host by the env prefix the probe carries. Claude-specific.
    _QUEUE_LOG_PATH_TEMPLATE: ClassVar[str] = "$MNGR_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl"

    _DIALOG_INDICATORS: tuple[DialogIndicator, ...] = (
        TrustDialogIndicator(),
        CustomApiKeyDialogIndicator(),
        ThemeSelectionIndicator(),
        EffortCalloutIndicator(),
        CostThresholdDialogIndicator(),
    )

    def _build_accept_marker_command(self) -> str:
        """Shell snippet printing the latest enqueue timestamp from Claude's transcript log.

        Claude's transcript event log records an ``enqueue`` event (an
        ``"operation":"enqueue"`` JSONL line) the instant a message enters its
        queue. This prints that event's ISO-8601 ``timestamp`` (empty if none
        yet) -- the lexicographically-monotonic "message accepted" token that
        ``send_enter_via_tmux_wait_for_hook`` baselines before Enter and watches
        for a newer value, confirming submission the moment the message is
        accepted rather than waiting on the (possibly slow) UserPromptSubmit
        hook. The Claude-specific log schema lives here so ``tui_utils`` stays
        agent-neutral; the env prefix evaluates the embedded
        ``$MNGR_AGENT_STATE_DIR`` on the host, and the backslash-escaped quotes
        are interpreted by the inner ``bash -c`` that runs the probe.
        """
        env_command_prefix = self.host.build_source_env_prefix(self)
        return (
            f"{env_command_prefix} cat {self._QUEUE_LOG_PATH_TEMPLATE} 2>/dev/null "
            f'| grep "\\"operation\\":\\"enqueue\\"," | tail -n 1 | jq -r .timestamp 2>/dev/null'
        )

    def _send_enter_and_validate(self, tmux_target: TmuxWindowTarget) -> None:
        # Claude wires a UserPromptSubmit hook that fires `tmux wait-for -S`
        # on the per-session channel; wait for it. If the hook misfires
        # (occasionally happens while another message is being processed),
        # fall back to checking the transcript log for a fresh enqueue.
        send_enter_via_tmux_wait_for_hook(
            self,
            tmux_target,
            wait_channel=f"mngr-submit-{self.session_name}",
            timeout_seconds=self.enter_submission_timeout_seconds,
            accept_marker_command=self._build_accept_marker_command(),
        )

    def get_live_output_path(self) -> Path:
        """Return the path to this agent's response-streaming buffer file.

        Written by the stream_snapshot.py watcher when
        streaming_snapshot_interval_seconds > 0. The first line is the uuid of
        the last complete assistant message; the remaining lines are the
        in-progress assistant text reverse-mapped to markdown.
        """
        return get_agent_claude_plugin_dir(self._get_agent_dir()) / "stream_buffer"

    def make_live_output_reader(self) -> LiveOutputReader:
        """Diff successive stream_buffer snapshots into incremental assistant-text deltas."""
        return SnapshotDeltaReader()

    def _preflight_send_message(self, tmux_target: TmuxWindowTarget) -> None:
        """Check for blocking dialogs before sending a message.

        Checks the permissions_waiting file (set by the PermissionRequest hook)
        and captures the tmux pane for known dialog indicators.
        Raises DialogDetectedError if any are found.
        """
        if self._check_file_exists(self._get_agent_dir() / "permissions_waiting"):
            raise DialogDetectedError(str(self.name), "permission dialog")

        content = self._capture_pane_content(tmux_target)
        if content is None:
            return

        for indicator in self._DIALOG_INDICATORS:
            if indicator.matches(content):
                raise DialogDetectedError(str(self.name), indicator.get_description())

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        """Wait for the agent to become ready, executing start_action then polling.

        Polls for the 'session_started' file that the SessionStart hook creates.
        This indicates Claude Code has started and is ready for input.

        Raises AgentStartError if the agent doesn't signal readiness within the timeout.
        """
        if timeout is None:
            timeout = _READY_SIGNAL_TIMEOUT_SECONDS

        # this file is removed when we start the agent, see assemble_command, and created by the SessionStart hook when the session is ready
        session_started_path = self._get_agent_dir() / "session_started"

        with log_span("Waiting for session_started file (timeout={}s)", timeout):
            # Run the start action (e.g., start the agent)
            with log_span("Calling start_action..."):
                super().wait_for_ready_signal(is_creating, start_action, timeout)

            # Poll for the session_started file (created by SessionStart hook)
            if poll_until(
                lambda: self._check_file_exists(session_started_path),
                timeout=timeout,
                poll_interval=0.05,
            ):
                return

            raise AgentStartError(
                str(self.name),
                f"Agent did not signal readiness within {timeout}s. "
                "This may indicate a trust dialog appeared or Claude Code failed to start.",
            )

    def _build_background_tasks_command(self, session_name: str, primary_window_name: str) -> str:
        """Build a shell command that starts the background tasks script.

        The background tasks script (provisioned to $MNGR_AGENT_STATE_DIR/commands/)
        handles both activity tracking and transcript export. It runs in the
        background while the tmux session is alive. ``primary_window_name`` is
        passed through so the response-streaming watcher captures the agent pane
        by window name rather than the literal :0 index (base-index agnostic).
        """
        script_path = "$MNGR_AGENT_STATE_DIR/commands/claude_background_tasks.sh"
        return f"( {script_path} {shlex.quote(session_name)} {shlex.quote(primary_window_name)} ) &"

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
        initial_message: str | None = None,
    ) -> CommandString:
        """Assemble command with --resume || --session-id format for session resumption.

        The command format is: 'claude --resume $SID args || claude --session-id UUID args'
        This allows users to hit 'up' and 'enter' in tmux to resume the session (--resume)
        or create it with that ID (--session-id). The resume path uses $MAIN_CLAUDE_SESSION_ID,
        resolved at runtime from the session tracking file (falling back to the agent UUID on
        first run).

        An activity updater is started in the background to keep the agent's activity
        timestamp up-to-date while the tmux session is alive.

        ``initial_message`` is accepted for interface compatibility; the
        interactive ClaudeAgent delivers ``--message`` content through
        ``send_message`` after the tmux pane is ready, not via the command
        line, so it is ignored here.
        """
        if command_override is not None:
            base = str(command_override)
        elif self.agent_config.command is not None:
            base = str(self.agent_config.command)
        else:
            raise NoCommandDefinedError(f"No command defined for agent type '{self.agent_type}'")

        # Use the agent ID as the stable UUID for session identification
        agent_uuid = str(self.id.get_uuid())

        # Build the additional arguments (cli_args from config + agent_args from CLI).
        # cli_args arrive already shell-safe; agent_args are raw argv and must be quoted
        # before being spliced into this shell-evaluated command (see ``quote_agent_args``).
        # A user ``--settings`` passes through verbatim: in normal mode mngr injects no
        # ``--settings`` of its own (its hooks live in the config-dir settings.json, which
        # Claude layers under the user's command-line ``--settings``), so there is nothing
        # to collide with. In use_env_config_dir mode mngr does inject its own ``--settings``
        # (see ``mngr_settings_arg`` below); a user ``--settings`` then collides with it
        # (Claude is last-wins) -- the accepted, documented limitation of that mode.
        cli_args = self.agent_config.cli_args
        all_extra_args = cli_args + quote_agent_args(agent_args)
        args_str = " ".join(all_extra_args) if all_extra_args else ""

        # Read the latest session ID from the tracking file written by the SessionStart hook.
        # This handles session replacement (e.g., exit plan mode, /clear, compaction) where
        # Claude Code creates a new session with a different UUID. Falls back to the agent UUID
        # if the tracking file doesn't exist (first run) or is empty (crash during write).
        sid_export = (
            f'_MNGR_READ_SID=$(cat "$MNGR_AGENT_STATE_DIR/claude_session_id" 2>/dev/null || true);'
            f' export MAIN_CLAUDE_SESSION_ID="${{_MNGR_READ_SID:-{agent_uuid}}}"'
        )

        # Build both command variants using the dynamic session ID.
        # Use $CLAUDE_CONFIG_DIR to find session files in the per-agent config dir
        # rather than ~/.claude/. In shared mode the var may be unset (so claude
        # resolves its default ~/.claude.json -- see modify_env_vars), so the
        # lookup falls back to $HOME/.claude where the shared session files live.
        # Session files on disk
        # are named "<session_id>.jsonl"; matching without the extension would
        # always miss, the && would short-circuit, and the silent || fallback at
        # the end of assemble_command would spawn a fresh `claude --session-id
        # <agent_uuid>` without surfacing any error -- so an adopted session
        # would appear to do nothing.
        # mngr injects its own --settings only in shared mode (the managed hooks file
        # written by _configure_agent_hooks). In isolated mode the hooks are baked into
        # the config-dir settings.json, so mngr adds no --settings here. Keyed off the
        # resolved predicate (not the deprecated use_env_config_dir alias) so it matches
        # the write gate in provision() for shared mode set via isolate_local_config_dir=False.
        mngr_settings_arg = f" {MANAGED_SETTINGS_LAUNCH_ARG}" if not self._is_isolated_config_dir() else ""
        resume_cmd = f'( find "${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}" -name "$MAIN_CLAUDE_SESSION_ID.jsonl" | grep . ) && {base}{mngr_settings_arg} --resume "$MAIN_CLAUDE_SESSION_ID"'
        create_cmd = f"{base}{mngr_settings_arg} --session-id {agent_uuid}"

        # Append additional args to both commands if present
        if args_str:
            resume_cmd = f"{resume_cmd} {args_str}"
            create_cmd = f"{create_cmd} {args_str}"

        # Build the environment exports
        # IS_SANDBOX is only set for remote hosts (not local)
        env_exports = f"export IS_SANDBOX=1 && {sid_export}" if not host.is_local else sid_export

        # Build the background tasks command (activity tracking + transcript export)
        session_name = self.session_name
        background_cmd = self._build_background_tasks_command(
            session_name, self.mngr_ctx.config.tmux.primary_window_name
        )

        # Combine: start background tasks, export env (including session ID), then run the main command (and make sure we get rid of the session started marker on each run so that wait_for_ready_signal works correctly for both new and resumed sessions)
        return CommandString(
            f"{background_cmd} {env_exports} && rm -rf $MNGR_AGENT_STATE_DIR/session_started && ( {resume_cmd} ) || {create_cmd}"
        )

    def on_before_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Validate preconditions before provisioning (read-only).

        This method performs read-only validation only. No writes to
        disk or interactive prompts -- actual setup happens in provision().

        For non-interactive local runs: validates that all known Claude
        startup dialogs are dismissed so we fail early with a clear message.
        Interactive and auto-approve runs skip these checks because
        provision() will handle them.

        The dialog-dismissal validation runs for any local agent (both config
        modes), since provision() dismisses dialogs in the user's global config in
        either mode. Remote agents have no local user config to validate against,
        so ``host.is_local`` gates the check.

        Also surfaces the ``use_env_config_dir`` deprecation: warns once if the
        old key is set, and raises early if it contradicts ``isolate_local_config_dir``.
        In shared mode it additionally rejects a user-supplied ``--settings`` that
        would collide with mngr's own managed ``--settings``.
        """
        config = self.agent_config

        if config.use_env_config_dir is not None:
            logger.warning(
                "The claude `use_env_config_dir` config option is deprecated; set `isolate_local_config_dir` "
                "(its inverse) instead. use_env_config_dir={} is being treated as isolate_local_config_dir={}.",
                config.use_env_config_dir,
                not config.use_env_config_dir,
            )
        # Resolve once here so a contradictory use_env_config_dir / isolate_local_config_dir
        # pairing fails early with a clear message (the call raises on conflict).
        config.resolve_isolate_local_config_dir()

        # In shared mode (local + not isolated) mngr injects its own `--settings` (the managed
        # hooks file), which would collide with a user-supplied `--settings` (Claude is last-wins).
        # mngr can't reliably merge them -- a `--settings` value may be inline JSON, not a file --
        # so fail fast and point at the supported alternatives. Remote agents are always isolated,
        # so this never applies to them.
        if not self._is_isolated_config_dir() and (
            _has_settings_flag(config.cli_args) or _has_settings_flag(options.agent_args)
        ):
            raise UserInputError(
                "Sharing the Claude config dir (isolate_local_config_dir=False) passes mngr's own "
                "`--settings` to claude (to load its hooks), which collides with the `--settings` you "
                "supplied via cli_args/agent_args. Put those settings in the agent type's "
                "`settings_overrides`, or set isolate_local_config_dir=True so mngr provisions a per-agent "
                "config dir and claude layers your `--settings` natively."
            )

        # Validate dialogs for non-interactive local runs so we fail early with
        # a clear message. Skip when auto_dismiss_dialogs is True (provision()
        # auto-dismisses) and for remote hosts (no local user config to validate).
        # Both config modes are validated -- provision() dismisses against the
        # user's global config in either mode.
        if (
            host.is_local
            and not mngr_ctx.is_interactive
            and not mngr_ctx.is_auto_approve
            and not config.auto_dismiss_dialogs
        ):
            transfer_mode = options.transfer_mode
            if transfer_mode in (TransferMode.GIT_WORKTREE, TransferMode.GIT_MIRROR):
                source_path = self._find_git_source_path(mngr_ctx.concurrency_group)
                trust_path = source_path if source_path is not None else self.work_dir
            else:
                trust_path = self.work_dir
            check_claude_dialogs_dismissed(self._dialog_dismissal_config_path(), trust_path)
        if not config.check_installation:
            logger.debug("Skipped claude installation check (check_installation=False)")
            return

        if not _has_api_credentials_available(host, options, config, mngr_ctx.concurrency_group):
            logger.warning(
                "No API credentials detected for Claude Code. The agent may fail to start.\n"
                "Provide credentials via one of:\n"
                "  - Set ANTHROPIC_API_KEY environment variable (use --pass-env ANTHROPIC_API_KEY)\n"
                "  - Run 'claude login' to create ~/.claude/.credentials.json"
            )

    def _dismiss_start_dialogs(
        self, host: OnlineHostInterface, options: CreateAgentOptions, mngr_ctx: MngrContext
    ) -> None:
        """Dismiss blocking Claude startup dialogs before the agent starts so they don't
        intercept tmux input. Acts on local hosts in either config mode (isolated
        writes to the user's global config that the per-agent dir inherits; shared
        writes to the global config claude reads directly). Remote hosts have no
        local user config to dismiss against, so they are skipped.
        ``auto_dismiss_dialogs`` silently approves, otherwise routes through
        ``interactively_dismiss_claude_dialogs`` (prompt/validate per mode).
        """
        config = self.agent_config
        if not host.is_local:
            return
        # Determine the source path for trust extension.
        source_path: Path | None = None
        if options.transfer_mode in (TransferMode.GIT_WORKTREE, TransferMode.GIT_MIRROR):
            source_path = self._find_git_source_path(mngr_ctx.concurrency_group)
        if config.auto_dismiss_dialogs:
            # Auto-approve all dialogs for agents that opt into dismissal.
            auto_dismiss_claude_dialogs(self._dialog_dismissal_config_path(), self.work_dir)
        else:
            # source_path=None (clone/no-git) means trust is prompted for work_dir.
            self.interactively_dismiss_claude_dialogs(source_path, mngr_ctx)

    def interactively_dismiss_claude_dialogs(self, source_path: Path | None, mngr_ctx: MngrContext) -> None:
        """Ensure all known Claude startup dialogs are dismissed in the global config.

        All dialogs that could intercept tmux input must be dismissed before
        starting an agent, otherwise mngr message will break. Writes to the
        user's global config to record intent. In isolated mode the per-agent
        config dir inherits these settings; in shared mode the agent's claude
        reads the global config directly (see ``_dialog_dismissal_config_path``).

        For auto-approve mode, silently dismisses all dialogs. For interactive
        mode, prompts the user for each undismissed dialog. For non-interactive
        mode, raises the appropriate error.

        source_path is the trusted source directory (for git-worktree/git-mirror modes).
        When None (rsync/none mode), trust is prompted for work_dir instead.
        """
        global_config_path = self._dialog_dismissal_config_path()
        trust_path = source_path if source_path is not None else self.work_dir

        if mngr_ctx.is_auto_approve:
            auto_dismiss_claude_dialogs(global_config_path, trust_path)
            return

        if not is_source_directory_trusted(global_config_path, trust_path):
            if not mngr_ctx.is_interactive or not _prompt_user_for_trust(trust_path):
                raise ClaudeDirectoryNotTrustedError(str(trust_path))
            add_claude_trust_for_path(global_config_path, trust_path)

        if not is_effort_callout_dismissed(global_config_path):
            if not mngr_ctx.is_interactive or not _prompt_user_for_effort_callout_dismissal():
                raise ClaudeEffortCalloutNotDismissedError()
            dismiss_effort_callout(global_config_path)

        if not is_onboarding_completed(global_config_path):
            if not mngr_ctx.is_interactive or not _prompt_user_for_onboarding_completion():
                raise ClaudeOnboardingNotCompletedError()
            complete_onboarding(global_config_path)

        # Note: bypassPermissionsModeAccepted is NOT checked here because Claude Code
        # periodically resets it to null in ~/.claude.json, causing repeated prompts.
        # The bypass-permissions warning is reliably suppressed by
        # skipDangerousModePermissionPrompt in settings.json instead.

    def on_after_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Adopt a session after provisioning so the agent's claude resumes existing context."""
        self.adopt_session(host, options, mngr_ctx)

    def adopt_session(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Adopt a session so the agent's claude resumes existing context.

        Delegates to :func:`~imbue.mngr.api.preservation.adopt_sessions`, which copies
        every ``--adopt`` session (``copy_explicit``) and the ``--from`` clone
        (``copy_clone``) into this agent, then resumes one (``resume``): the clone when
        ``--from`` is given, otherwise the last ``--adopt`` value. The rest are left
        available in the new agent's session switcher. With neither option set nothing
        is adopted (fresh start). Claude can only resume a single session at a time.

        Each ``copy_explicit`` call copies one named session's source project dir into the
        destination's encoded project dir, deduplicating by source project dir name across
        calls (multiple named sessions may share one project dir).

        Destination resolution depends on ``isolate_local_config_dir``:
        - Default (``True``): copies into the per-agent config dir at
          ``$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/projects/<encoded>/``.
        - Shared (``False``): copies into the user's shared
          ``$CLAUDE_CONFIG_DIR/projects/<encoded-work_dir>/``. Per spec decision
          4c this is the only sanctioned mngr write to the user's config dir
          in shared mode, and it only adds new project subdirs -- it never
          modifies existing user files.
        """
        # Shared across copy_explicit calls so a project dir holding several named
        # sessions is copied only once.
        copied_project_dirs: set[str] = set()

        adopt_sessions(
            options.adopt_session,
            options.source_agent_state_location,
            copy_explicit=lambda arg: self._copy_explicit_session(host, arg, copied_project_dirs),
            copy_clone=lambda location: self._adopt_cloned_session(host, location),
            resume=lambda session_id: self._finalize_adopted_session(
                host, self._dest_adopted_project_dir(), session_id
            ),
        )

    def _copy_explicit_session(self, host: OnlineHostInterface, arg: str, copied_project_dirs: set[str]) -> str:
        """Resolve one explicit ``--adopt`` value and copy its project dir into this agent.

        ``copied_project_dirs`` is shared across calls so a project dir holding several
        named sessions is copied only once. Returns the resolved session id; the
        orchestrator decides which session is resumed.
        """
        session_id, source_project_dir = _resolve_adopt_session(arg, self.mngr_ctx)
        if source_project_dir.name not in copied_project_dirs:
            with log_span("Adopting session {}", session_id):
                host.copy_directory(host, source_project_dir, self._dest_adopted_project_dir())
            copied_project_dirs.add(source_project_dir.name)
        return session_id

    def _dest_adopted_project_dir(self) -> Path:
        """Return the encoded project dir adopted sessions are placed under.

        Claude Code organizes sessions by encoded working directory path, so adopted
        sessions live under the project dir matching this agent's work_dir; see
        ``_resolve_work_dir_on_host`` for why we resolve through symlinks.
        """
        dest_project_name = encode_claude_project_dir_name(self._resolve_work_dir_on_host())
        return self.get_claude_config_dir() / "projects" / dest_project_name

    def _finalize_adopted_session(
        self,
        host: OnlineHostInterface,
        adopted_project_dir: Path,
        adopted_session_id: str,
    ) -> None:
        """Drop the stale ``sessions-index.json`` (claude rebuilds it; the
        rsynced one points at source paths and blocks lookup of the
        adopted session) and write ``claude_session_id`` so the startup
        ``claude --resume "$MAIN_CLAUDE_SESSION_ID"`` targets it.
        """
        stale_index = adopted_project_dir / "sessions-index.json"
        host.execute_idempotent_command(f"rm -f {shlex.quote(str(stale_index))}", timeout_seconds=5.0)
        host.write_text_file(self._get_agent_dir() / "claude_session_id", adopted_session_id)

    def _adopt_cloned_session(self, host: OnlineHostInterface, source_location: HostLocation) -> str | None:
        """Rewire the rsynced plugin/ so ``claude --resume`` finds the source's session.

        After ``_transfer_source_plugin_data`` rsyncs the source's
        ``plugin/``, the JSONL is filed under the *source* agent's
        encoded work_dir; claude on the destination searches under
        ``projects/<dest-encoded-work-dir>/`` so it can't see it.

        This method discovers the source's active session source-side
        (``ls -t``, so we can bail without a second destination round-trip
        if there's nothing to adopt), carries ``claude_session_id_history``
        forward, renames the project subdir to the destination's encoded
        work_dir, and returns the discovered session id (the caller resumes it).

        A ``--from`` clone is a workspace clone; carrying the source's session
        forward is a bonus, so a source with no resumable session JSONL warns and
        returns ``None`` (the caller then resumes the last ``--adopt`` instead, or
        starts fresh).

        Session id comes from the JSONL filename, not the source's
        ``claude_session_id`` file: ``claude -p`` ignores ``--session-id``
        and auto-generates its own, so the file (defaulted to the agent
        UUID by the SessionStart hook) disagrees with the JSONL on disk.
        """
        source_host = source_location.host
        source_state_dir = source_location.path

        # Carry the source's claude_session_id_history forward so the
        # destination's history reflects the prior run.
        source_history_path = source_state_dir / "claude_session_id_history"
        if source_host.path_exists(source_history_path):
            host.write_text_file(
                self._get_agent_dir() / "claude_session_id_history",
                source_host.read_text_file(source_history_path),
            )

        # Layout: plugin/claude/anthropic/projects/<encoded-work-dir>/<sid>.jsonl.
        # The shallow ``*/*.jsonl`` glob excludes nested subagent transcripts
        # at ``<sid>/subagents/agent-X.jsonl``.
        source_projects_dir = source_state_dir / _AGENT_CLAUDE_PROJECTS_RELPATH
        latest_on_source = source_host.execute_idempotent_command(
            f"ls -t {shlex.quote(str(source_projects_dir))}/*/*.jsonl 2>/dev/null | head -n1",
            timeout_seconds=5.0,
        )
        if not (latest_on_source.success and latest_on_source.stdout.strip()):
            # A ``--from`` clone is a workspace clone; carrying the source's session
            # forward is a bonus, so nothing to resume (no session, or the ``ls``
            # failed) is not fatal -- warn and let the caller fall back.
            logger.warning(
                "Clone adopt: no session JSONL found at source {} (ls success={}, stderr={!r}); "
                "not resuming the clone's conversation.",
                source_projects_dir,
                latest_on_source.success,
                latest_on_source.stderr.strip(),
            )
            return None
        latest_path = Path(latest_on_source.stdout.strip())
        source_project_name = latest_path.parent.name
        adopted_session_id = latest_path.stem

        # Rekey the source-encoded project subdir onto the destination's encoded
        # work_dir. The target dir may already exist (e.g. an explicit ``--adopt``
        # session was copied into it first), so merge the source subdir's *files*
        # into the target rather than moving the whole dir. Refuse only on a
        # per-file collision (same filename in both): that means real data would
        # be lost, while distinct session JSONLs coexist cleanly in one project dir.
        dest_projects_dir = self._get_agent_dir() / _AGENT_CLAUDE_PROJECTS_RELPATH
        dest_project_name = encode_claude_project_dir_name(self._resolve_work_dir_on_host())
        if source_project_name != dest_project_name:
            source_subdir = dest_projects_dir / source_project_name
            target_dir = dest_projects_dir / dest_project_name
            self._merge_project_subdir(host, source_subdir, target_dir)

        return adopted_session_id

    def _merge_project_subdir(self, host: OnlineHostInterface, source_subdir: Path, target_dir: Path) -> None:
        """Non-destructively merge ``source_subdir``'s files into ``target_dir``.

        Moves each entry from the source subdir into the (possibly pre-existing)
        target dir, then removes the now-empty source subdir. Raises
        :class:`AgentStartError` on a per-file collision (same filename in both),
        which would otherwise silently lose data.
        """
        entry_names = [Path(entry.path).name for entry in host.list_directory(source_subdir)]
        collisions = sorted(name for name in entry_names if host.path_exists(target_dir / name))
        if collisions:
            raise AgentStartError(
                str(self.name),
                f"Refusing to merge cloned project subdir {source_subdir} into {target_dir}: "
                f"file(s) {collisions} already exist in the target, so the cloned agent "
                "cannot resume the source's session without overwriting existing data.",
            )
        # Move each entry by name (portable across BSD/GNU mv -- no -t/-n flags),
        # then drop the now-empty source subdir.
        move_steps = [f"mkdir -p {shlex.quote(str(target_dir))}"]
        for name in entry_names:
            move_steps.append(f"mv {shlex.quote(str(source_subdir / name))} {shlex.quote(str(target_dir / name))}")
        move_steps.append(f"rmdir {shlex.quote(str(source_subdir))}")
        merge_result = host.execute_idempotent_command(" && ".join(move_steps), timeout_seconds=10.0)
        if not merge_result.success:
            raise AgentStartError(
                str(self.name),
                f"Failed to merge cloned project subdir {source_subdir} into {target_dir}: "
                f"{merge_result.stderr.strip()}",
            )


def _claude_preserved_items(is_shared_config: bool) -> list[PreservedItem]:
    """Return the files to preserve from a Claude agent's state directory.

    Paths are relative to the agent state directory and are identical for the
    online and offline preservation paths:

    - ``plugin/claude/anthropic/projects`` -- the per-agent Claude config dir's
      session JSONLs. Skipped in shared (``isolate_local_config_dir=False``) mode, where projects
      live in the user's persistent ``$CLAUDE_CONFIG_DIR`` (not under the agent
      state dir, and shared across all of the user's projects); they survive
      destruction already and must not be duplicated per-agent.
    - ``logs/claude_transcript`` -- the raw, agent-native transcript.
    - ``events/claude/common_transcript`` -- the common (agent-agnostic) transcript.
    - ``claude_session_id_history`` -- the session-id history file.
    """
    items: list[PreservedItem] = []
    if not is_shared_config:
        items.append(PreservedItem(rel_path=_AGENT_CLAUDE_PROJECTS_RELPATH.as_posix(), kind=FileType.DIRECTORY))
    items.append(PreservedItem(rel_path="logs/claude_transcript", kind=FileType.DIRECTORY))
    items.append(PreservedItem(rel_path="events/claude/common_transcript", kind=FileType.DIRECTORY))
    items.append(PreservedItem(rel_path="claude_session_id_history", kind=FileType.FILE))
    return items


def _should_preserve_sessions(ref: DiscoveredAgent) -> bool:
    """Check whether an agent's config has preserve_sessions_on_destroy enabled.

    Reads from certified_data (the raw data.json) so it works for offline
    hosts without needing to resolve the full agent type.
    """
    agent_config = ref.certified_data.get("agent_config", {})
    return bool(agent_config.get("preserve_sessions_on_destroy"))


def _claude_items_to_preserve_for_discovered_agent(ref: DiscoveredAgent) -> list[PreservedItem] | None:
    """Return the items to preserve for a discovered (offline) Claude agent, or None to skip it."""
    if not _should_preserve_sessions(ref):
        return None
    agent_config = ref.certified_data.get("agent_config", {})
    # Mirror ClaudeAgentConfig.resolve_isolate_local_config_dir on the raw record:
    # the deprecated ``use_env_config_dir`` (when set) is the inverse of
    # ``isolate_local_config_dir`` and takes precedence; otherwise fall back to
    # ``isolate_local_config_dir`` (default True -> isolated). An agent record
    # that set neither key -- or one created before either existed -- is treated
    # as isolated, so its per-agent ``projects/`` is preserved.
    legacy_use_env = agent_config.get("use_env_config_dir")
    if legacy_use_env is not None:
        is_shared_config = bool(legacy_use_env)
    else:
        is_shared_config = not agent_config.get("isolate_local_config_dir", True)
    return _claude_preserved_items(is_shared_config=is_shared_config)


def _generate_claude_home_settings() -> dict[str, Any]:
    """default contents for ~/.claude/settings.json"""
    return {"skipDangerousModePermissionPrompt": True}


def _generate_claude_json(
    version: str | None, current_time: datetime | None = None, disable_auto_update: bool = True
) -> dict[str, Any]:
    """default contents for .claude.json

    ``disable_auto_update`` sets the ``autoUpdates`` flag: the authoritative lever
    is the ``DISABLE_AUTOUPDATER`` env var (set in ``modify_env_vars``), but the
    generated default mirrors the resolved update policy so a fresh config is
    internally consistent.
    """
    if version is None:
        version = "2.1.50"
    if current_time is None:
        current_time = datetime.now(timezone.utc)
        current_time_str = current_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        current_time_millis = int(current_time.timestamp() * 1000)
        cache_time_millis = current_time_millis + 50 + random.random() * 1000
        change_log_time_millis = cache_time_millis + 500 + random.random() * 5000
    else:
        current_time_str = current_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        cache_time_millis = int(current_time.timestamp() * 1000) + 50
        change_log_time_millis = cache_time_millis + 500
    return {
        "numStartups": 1,
        "installMethod": "native",
        "autoUpdates": not disable_auto_update,
        "firstStartTime": current_time_str,
        "opusProMigrationComplete": True,
        "sonnet1m45MigrationComplete": True,
        "clientDataCache": {"data": None, "timestamp": cache_time_millis},
        "cachedChromeExtensionInstalled": False,
        "changelogLastFetched": change_log_time_millis,
        "hasCompletedOnboarding": True,
        "lastOnboardingVersion": version,
        "lastReleaseNotesSeen": version,
        "effortCalloutDismissed": True,
        "bypassPermissionsModeAccepted": True,
        "officialMarketplaceAutoInstallAttempted": True,
        "officialMarketplaceAutoInstalled": True,
        "autoUpdatesProtectedForNative": True,
        "hasAcknowledgedCostThreshold": True,
    }


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the claude agent type."""
    return ("claude", ClaudeAgent, ClaudeAgentConfig)


def _waiting_reason(agent: AgentInterface, host: OnlineHostInterface) -> WaitingReason | None:
    """Return why the agent is waiting based on marker files, or None.

    Checks the agent state directory for marker files rather than calling
    get_lifecycle_state() (which involves tmux/ps SSH commands), then delegates the
    decision to the shared ``classify_waiting_reason`` so this and the lifecycle
    promotion stay in lockstep. ``permissions_waiting`` is only read when ``active``
    is present, both to short-circuit the idle case and because the classifier
    ignores the permission signal when the agent is not in a turn.
    """
    agent_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    is_active = host.path_exists(agent_dir / "active")
    is_blocked_on_permission = is_active and host.path_exists(agent_dir / "permissions_waiting")
    return classify_waiting_reason(is_active, is_blocked_on_permission)


@hookimpl
def agent_field_generators() -> tuple[str, dict[str, Callable[[AgentInterface, OnlineHostInterface], Any]]] | None:
    """Expose Claude-specific agent fields for listing."""
    return ("claude", {"waiting_reason": _waiting_reason})


@hookimpl
def on_before_host_destroy(host: HostInterface, mngr_ctx: MngrContext) -> None:
    """Preserve Claude session files from the host's volume before it is destroyed.

    When a host goes offline and is destroyed without calling agent.on_destroy(),
    session data still lives on the host's persisted volume. The shared
    :func:`preserve_host_agents_on_destroy` reads the declared files straight off
    that volume (when the host surfaces one) for each Claude agent that opted in.
    """
    preserve_host_agents_on_destroy(
        host, mngr_ctx, AgentTypeName("claude"), _claude_items_to_preserve_for_discovered_agent
    )


@hookimpl
def on_before_create(args: OnBeforeCreateArgs, mngr_ctx: MngrContext) -> OnBeforeCreateArgs | None:
    """Fail-fast pre-resolution of claude ``--adopt`` session ids (see ``run_adopt_session_preflight``)."""
    run_adopt_session_preflight(
        args.agent_options.agent_type,
        args.agent_options.adopt_session,
        mngr_ctx,
        ClaudeAgent,
        lambda session_arg: _resolve_adopt_session(session_arg, mngr_ctx),
    )
    return None


@hookimpl
def get_files_for_deploy(
    mngr_ctx: MngrContext,
    include_user_settings: bool,
    include_project_settings: bool,
    repo_root: Path,
) -> dict[Path, Path | str]:
    """Register claude-specific files for scheduled deployments.

    Files use ~/.claude/ prefix paths and are staged to $HOME/.claude/ in
    the deploy image. At runtime, mngr create triggers provisioning which
    copies these into the per-agent config directory (CLAUDE_CONFIG_DIR).

    Always includes settings.json and .claude.json (using generated defaults
    when local files are unavailable or user settings are excluded).
    When include_user_settings is True, also includes keybindings.json,
    skills/, agents/, commands/, plugins/, and credentials.
    """
    files: dict[Path, Path | str] = {}

    local_claude_dir = get_user_claude_config_dir()
    deploy_ctx = ProvisioningContext(is_unattended=True, copy_project_config_from=None)
    deploy_config = ClaudeAgentConfig()

    # settings.json always ships (generated, not a direct copy). There is no agent
    # instance here, so the unattended gate reads the config field directly -- the
    # field-based equivalent of Claude's is_unattended_enabled().
    files[Path("~/.claude/settings.json")] = _build_settings_json(
        local_claude_dir,
        deploy_config,
        deploy_ctx,
        sync_local=include_user_settings,
        is_unattended=deploy_config.auto_allow_permissions,
        allow_narrowing=mngr_ctx.config.allow_settings_key_assignment_narrowing,
    )

    # Always ship .claude.json to $HOME/.claude/ in the deploy image.
    # we set the time to a constant for better caching:
    FIXED_TIME = datetime(2026, 2, 23, 3, 4, 7, tzinfo=timezone.utc)
    claude_json_data = _build_claude_json(
        work_dir=repo_root,
        config=deploy_config,
        ctx=deploy_ctx,
        sync_local=False,
        version=None,
        current_time=FIXED_TIME,
    )
    # also inject our API key here, since deployed versions need it
    approve_api_key_for_claude(claude_json_data)
    files[Path("~/.claude.json")] = json.dumps(claude_json_data, indent=2) + "\n"

    if include_user_settings:
        # Collect individual sync files (e.g. keybindings.json)
        for file_name in _CLAUDE_HOME_SYNC_FILES:
            file_path = local_claude_dir / file_name
            if file_path.exists():
                files[Path("~/.claude") / file_name] = file_path

        # Collect directory contents (skills, agents, commands, plugins)
        for dir_name in _CLAUDE_HOME_SYNC_DIRS:
            dir_path = local_claude_dir / dir_name
            if not dir_path.exists():
                continue
            for file_path in dir_path.rglob("*"):
                if not file_path.is_file():
                    continue
                relative_path = file_path.relative_to(local_claude_dir)
                # Rewrite installPath values at build time to use the sentinel prefix,
                # so the runtime fixup can rewrite them to the actual config_dir
                # without needing to know the build machine's home directory
                if relative_path == _INSTALLED_PLUGINS_RELATIVE_PATH:
                    content = _rewrite_installed_plugins_paths(
                        file_path.read_text(), local_claude_dir, Path(_INSTALLED_PLUGINS_SENTINEL_PREFIX)
                    )
                    files[Path("~/.claude") / relative_path] = content
                elif relative_path == _KNOWN_MARKETPLACES_RELATIVE_PATH:
                    content = _rewrite_known_marketplaces_paths(
                        file_path.read_text(), local_claude_dir, Path(_INSTALLED_PLUGINS_SENTINEL_PREFIX)
                    )
                    files[Path("~/.claude") / relative_path] = content
                else:
                    files[Path("~/.claude") / relative_path] = file_path

        # ~/.claude/.credentials.json (OAuth tokens)
        credentials = local_claude_dir / ".credentials.json"
        if credentials.exists():
            files[Path("~/.claude/.credentials.json")] = credentials

    if include_project_settings:
        # Include unversioned project-specific claude settings (e.g.
        # .claude/settings.local.json) from the repo root directory.
        # These are typically gitignored and contain project-specific config.
        project_claude_dir = repo_root / ".claude"
        if project_claude_dir.is_dir():
            for file_path in project_claude_dir.rglob("*.local.*"):
                if file_path.is_file():
                    relative_path = file_path.relative_to(repo_root)
                    files[Path(str(relative_path))] = file_path

    return files


@hookimpl
def modify_env_vars_for_deploy(
    mngr_ctx: MngrContext,
    env_vars: dict[str, str],
) -> None:
    if "ANTHROPIC_API_KEY" not in env_vars:
        deploy_ctx = ProvisioningContext(is_unattended=True, copy_project_config_from=None)
        user_claude_json_data = _build_claude_json(
            work_dir=Path("."), config=ClaudeAgentConfig(), ctx=deploy_ctx, sync_local=True, version=None
        )
        token = user_claude_json_data.get("primaryApiKey", "") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not token:
            raise UserInputError(
                "ANTHROPIC_API_KEY environment variable is not set and no API key found in ~/.claude.json. "
                "You must provide credentials to authenticate with Claude Code in order for the deployment to work."
            )
        env_vars["ANTHROPIC_API_KEY"] = token
    env_vars["IS_SANDBOX"] = "1"


def approve_api_key_for_claude(
    data: dict[str, Any],
    host: OnlineHostInterface | None = None,
    options: CreateAgentOptions | None = None,
) -> None:
    """Approve every reachable ANTHROPIC_API_KEY so claude doesn't block on the custom-key dialog.

    Claude challenges any ``ANTHROPIC_API_KEY`` it sees in env that doesn't match either
    ``primaryApiKey`` in its config or an entry in ``customApiKeyResponses.approved``. The
    challenge is interactive (TUI prompt), which deadlocks ``mngr``'s ``wait_for_ready_signal``.

    Sources we consult, in priority order, mirroring ``_has_api_credentials_available``:

    - ``os.environ.get("ANTHROPIC_API_KEY")`` -- the running mngr process (e.g. ``mngr_imbue_cloud``
      injects the LiteLLM key here via ``subprocess_env`` before calling ``mngr create``).
    - ``options.environment.env_vars`` -- explicit ``--env`` / ``--pass-env`` from the CLI.
    - ``host.get_env_var("ANTHROPIC_API_KEY")`` -- the *target host's* env file, populated by
      ``_write_host_env_vars`` from ``--host-env``, ``--pass-host-env``, and ``--host-env-file``.
      The last one is critical: minds passes the workspace ``.env`` via ``--host-env-file`` and
      its ``ANTHROPIC_API_KEY`` only ever lives there, never in ``os.environ``. Without consulting
      the host env, the approval was a no-op for the LOCAL/Docker path (see PR thread for
      assistant2 reproduction).
    - ``primaryApiKey`` in the user's ``~/.claude.json``.

    ``host`` and ``options`` default to ``None`` because :func:`approve_api_key_for_claude` is
    also called from the deploy-image path (``_collect_files_for_deploy``) where there is no
    host yet and the only credential source is ``os.environ`` / the user's claude config.
    """
    keys_to_approve: list[str] = []

    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        keys_to_approve.append(env_key)

    if options is not None:
        for env_var in options.environment.env_vars:
            if env_var.key == "ANTHROPIC_API_KEY" and env_var.value:
                keys_to_approve.append(env_var.value)

    if host is not None:
        host_key = host.get_env_var("ANTHROPIC_API_KEY") or ""
        if host_key:
            keys_to_approve.append(host_key)

    user_config = read_claude_config(find_user_config_in_isolated_mode())
    conf_key = user_config.get("primaryApiKey", "")
    if conf_key:
        keys_to_approve.append(conf_key)

    if not keys_to_approve:
        return

    approved_section = data.setdefault("customApiKeyResponses", {})
    approved_list = list(approved_section.get("approved", []))
    for key in keys_to_approve:
        suffix = key[-20:]
        if suffix not in approved_list:
            approved_list.append(suffix)
    approved_section["approved"] = approved_list
    approved_section["rejected"] = []
