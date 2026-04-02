from __future__ import annotations

import copy
import getpass
import hashlib
import importlib.resources
import json
import os
import random
import shlex
import tempfile
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Final

import click
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.concurrency_group import InvalidConcurrencyGroupStateError
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.thread_utils import ObservableThread
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import ConfigError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import NoCommandDefinedError
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.errors import UserInputError
from imbue.mngr.hosts.common import is_macos
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import FileTransferSpec
from imbue.mngr.interfaces.data_types import RelativePath
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.plugins.hookspecs import OnBeforeCreateArgs
from imbue.mngr.plugins.hookspecs import OptionStackItem
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.primitives import TransferMode
from imbue.mngr.utils.git_utils import find_git_common_dir
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_claude import hookimpl
from imbue.mngr_claude import resources as _claude_resources
from imbue.mngr_claude.claude_config import ClaudeDirectoryNotTrustedError
from imbue.mngr_claude.claude_config import ClaudeEffortCalloutNotDismissedError
from imbue.mngr_claude.claude_config import ClaudeOnboardingNotCompletedError
from imbue.mngr_claude.claude_config import acknowledge_cost_threshold
from imbue.mngr_claude.claude_config import add_claude_trust_for_path
from imbue.mngr_claude.claude_config import auto_dismiss_claude_dialogs
from imbue.mngr_claude.claude_config import build_readiness_hooks_config
from imbue.mngr_claude.claude_config import check_claude_dialogs_dismissed
from imbue.mngr_claude.claude_config import complete_onboarding
from imbue.mngr_claude.claude_config import dismiss_effort_callout
from imbue.mngr_claude.claude_config import encode_claude_project_dir_name
from imbue.mngr_claude.claude_config import find_project_config
from imbue.mngr_claude.claude_config import get_claude_config_path
from imbue.mngr_claude.claude_config import is_effort_callout_dismissed
from imbue.mngr_claude.claude_config import is_onboarding_completed
from imbue.mngr_claude.claude_config import is_source_directory_trusted
from imbue.mngr_claude.claude_config import merge_hooks_config
from imbue.mngr_claude.claude_config import read_claude_config
from imbue.mngr_claude.claude_config import remove_claude_trust_for_path

_READY_SIGNAL_TIMEOUT_SECONDS: Final[float] = 10.0

# Paths within ~/.claude/ to sync to the per-agent config dir.
# Used by both get_files_for_deploy() and provision() to ensure consistency.
_CLAUDE_HOME_SYNC_DIRS: Final[tuple[str, ...]] = ("skills", "agents", "commands", "plugins")

_INSTALLED_PLUGINS_RELATIVE_PATH: Final[Path] = Path("plugins") / "installed_plugins.json"

_INSTALLED_PLUGINS_SENTINEL_PREFIX: Final[str] = "/__mngr_plugins_source__"
"""Sentinel prefix written into installPath values at deploy build time.

At build time, ``get_files_for_deploy`` rewrites absolute local paths
(e.g. /home/user/.claude/plugins/cache/...) to use this sentinel prefix.
At runtime, the fixup rewrites the sentinel to the actual per-agent config dir.
This avoids depending on the build machine's home directory path.
"""


def _resolve_adopt_session(adopt_session_arg: str) -> tuple[str, Path]:
    """Resolve an --adopt-session argument to a (session_id, project_dir) pair.

    Accepts either:
    - A path to a .jsonl file (e.g. ~/.claude/projects/foo/abc123.jsonl)
    - A session ID string (searched in both $CLAUDE_CONFIG_DIR/projects/ and ~/.claude/projects/)

    Returns (session_id, source_project_dir).
    """
    if adopt_session_arg.endswith(".jsonl"):
        session_file = Path(adopt_session_arg).resolve()
        if not session_file.exists():
            raise UserInputError(f"Session file not found: {session_file}")
        return session_file.stem, session_file.parent

    # Search by session ID in $CLAUDE_CONFIG_DIR first, then fall back to ~/.claude/.
    # We check both because when running inside an mngr agent, CLAUDE_CONFIG_DIR
    # points to the agent's isolated config dir, but the user's sessions are
    # in ~/.claude/. In non-agent contexts, CLAUDE_CONFIG_DIR may point to a
    # custom config dir that also has sessions.
    default_config_dir = Path.home() / ".claude"
    search_dirs: list[Path] = []
    resolved_dirs: list[Path] = []
    env_config_dir_str = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_config_dir_str:
        env_projects_dir = Path(env_config_dir_str) / "projects"
        search_dirs.append(env_projects_dir)
        resolved_dirs.append(env_projects_dir.resolve())
    default_projects_dir = default_config_dir / "projects"
    if default_projects_dir.resolve() not in resolved_dirs:
        search_dirs.append(default_projects_dir)

    matches: list[Path] = []
    searched: list[Path] = []
    for projects_dir in search_dirs:
        if projects_dir.exists():
            searched.append(projects_dir)
            matches.extend(projects_dir.glob(f"*/{adopt_session_arg}.jsonl"))

    if not searched:
        dirs_str = " or ".join(str(d) for d in search_dirs)
        raise UserInputError(f"No projects directory found at {dirs_str}. Cannot find session to adopt.")
    if not matches:
        dirs_str = " or ".join(str(d) for d in searched)
        raise UserInputError(
            f"Session {adopt_session_arg} not found in {dirs_str}. "
            "Check that the session ID is correct, or pass a path to the .jsonl file."
        )
    if len(matches) > 1:
        match_list = "\n".join(f"  {m}" for m in matches)
        raise UserInputError(
            f"Session {adopt_session_arg} found in multiple project directories:\n{match_list}\n"
            "Pass the full path to the .jsonl file to specify which one."
        )

    return adopt_session_arg, matches[0].parent


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
    check_installation: bool = Field(
        default=True,
        description="Check if claude is installed (if False, assumes it is already present)",
    )
    # FIXME: when the version is pinned, we should, during provisioning, ensure that the auto-updates are disabled. This means doing the following:
    #  - for local, check that "DISABLE_AUTOUPDATER=1" or "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" are set in the local claude settings (~/.claude/settings.json) and warn if not
    #  - for remote, just automatically add these env vars to the agent environment:
    #       export DISABLE_AUTOUPDATER=1 && export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 && export CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1
    #    this should be done by adding a new callback ("get_provision_env_vars") for agents (like get_provision_file_transfers) that allows us to define additional environment variables
    #    that function ("get_provision_env_vars") should be defined on our claude agent below, and should be called from Host::_collect_agent_env_vars in order to collect them all
    version: str | None = Field(
        default=None,
        description="Pin the Claude Code version to install (e.g., '2.1.50'). "
        "When set, installation uses this specific version and provisioning verifies the installed version matches. "
        "If None, uses the latest available version.",
    )
    auto_dismiss_dialogs: bool = Field(
        default=False,
        description="Automatically dismiss all Claude startup dialogs (trust, effort callout, onboarding) "
        "before startup. When False, the interactive flow prompts.",
    )
    settings_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value overrides merged into settings.json at provisioning time. "
        "Example: {'model': 'opus[1m]', 'fastMode': True}.",
    )
    emit_common_transcript: bool = Field(
        default=True,
        description="Emit a common, agent-agnostic transcript alongside the raw Claude transcript. "
        "When enabled, a background process converts raw transcript events into a common format at "
        "events/claude/common_transcript/events.jsonl. The common format includes user messages, "
        "assistant messages, and tool call/result summaries.",
    )


class ProvisioningContext(FrozenModel):
    """Runtime context derived from host type and transfer mode."""

    is_unattended: bool = Field(description="Agent runs without user interaction (remote/deploy)")
    copy_project_config_from: Path | None = Field(
        default=None, description="Source dir to copy project config from (worktree mode)"
    )


_ALWAYS_CLAUDE_JSON_FLAGS: Final[Mapping[str, bool]] = {"hasAcknowledgedCostThreshold": True}
_UNATTENDED_CLAUDE_JSON_FLAGS: Final[Mapping[str, bool]] = {
    "bypassPermissionsModeAccepted": True,
    "effortCalloutDismissed": True,
    "hasCompletedOnboarding": True,
}
_UNATTENDED_SETTINGS_FLAGS: Final[Mapping[str, Any]] = {
    "skipDangerousModePermissionPrompt": True,
    # fastMode off by default in unattended mode (API limitation)
    "fastMode": False,
}


@pure
def compute_claude_json_flags(ctx: ProvisioningContext) -> Mapping[str, bool]:
    """Compute .claude.json flags based on provisioning context."""
    if ctx.is_unattended:
        return {**_ALWAYS_CLAUDE_JSON_FLAGS, **_UNATTENDED_CLAUDE_JSON_FLAGS}
    return dict(_ALWAYS_CLAUDE_JSON_FLAGS)


@pure
def compute_settings_json_flags(ctx: ProvisioningContext) -> Mapping[str, Any]:
    """Compute settings.json flags based on provisioning context."""
    if ctx.is_unattended:
        return dict(_UNATTENDED_SETTINGS_FLAGS)
    return {}


@pure
def should_trust_work_dir(config: ClaudeAgentConfig, ctx: ProvisioningContext) -> bool:
    """Determine whether work_dir should be auto-trusted."""
    return ctx.is_unattended or config.auto_dismiss_dialogs


@pure
def _rewrite_installed_plugins_paths(content: str, source_claude_dir: Path, target_config_dir: Path) -> str:
    """Rewrite installPath values in installed_plugins.json for a target config dir.

    Rebases absolute paths from source_claude_dir onto target_config_dir
    so that Claude Code can find plugin files in the per-agent config dir.
    Raises ConfigError if any installPath doesn't start with the expected prefix.
    """
    data: dict[str, Any] = json.loads(content)
    source_prefix = str(source_claude_dir) + "/"
    for plugin_name, plugin_entries in data.get("plugins", {}).items():
        for entry in plugin_entries:
            install_path = entry.get("installPath", "")
            if not install_path.startswith(source_prefix):
                raise ConfigError(
                    f"installed_plugins.json: plugin {plugin_name!r} has installPath {install_path!r} "
                    f"which does not start with expected prefix {source_prefix!r}"
                )
            relative = install_path[len(source_prefix) :]
            entry["installPath"] = str(target_config_dir / relative)
    return json.dumps(data, indent=2) + "\n"


def _build_settings_json(
    source_claude_dir: Path,
    config: ClaudeAgentConfig,
    ctx: ProvisioningContext,
    sync_local: bool,
) -> str:
    """Build settings.json content for per-agent config dirs.

    Uses the local file as a base when sync_local is True and the file exists,
    otherwise uses generated defaults. Applies context-dependent flags
    (e.g. skipDangerousModePermissionPrompt for unattended) and user overrides.
    """
    source = source_claude_dir / "settings.json"
    if sync_local and source.exists():
        try:
            data: dict[str, Any] = json.loads(source.read_text())
        except json.JSONDecodeError:
            logger.warning("Corrupt settings.json at {}, using defaults", source)
            data = _generate_claude_home_settings()
    else:
        data = _generate_claude_home_settings()
    data.update(compute_settings_json_flags(ctx))
    data.update(config.settings_overrides)
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
    if sync_local:
        local_config = read_claude_config(get_claude_config_path())
        data: dict[str, Any] = (
            local_config if local_config else _generate_claude_json(version, current_time=current_time)
        )
    else:
        data = _generate_claude_json(version, current_time=current_time)

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


def _install_claude(host: OnlineHostInterface, version: str | None = None) -> None:
    """Install claude on the host using the official installer.

    Downloads the install script to a temp file, runs it, then verifies
    the binary exists. When version is specified, passes it as a positional
    argument (e.g., 'bash /tmp/install_claude.sh 2.1.50').
    """
    version_arg = f" {shlex.quote(version)}" if version else ""
    steps = [
        "curl --version",
        "curl -fsSL https://claude.ai/install.sh -o /tmp/install_claude.sh",
        f"bash /tmp/install_claude.sh{version_arg}",
        "rm -f /tmp/install_claude.sh",
        f"test -x {CLAUDE_INSTALL_PATH}/claude",
        f"""grep -qF 'export PATH="{CLAUDE_INSTALL_PATH}:$PATH"' ~/.bashrc 2>/dev/null || echo 'export PATH="{CLAUDE_INSTALL_PATH}:$PATH"' >> ~/.bashrc""",
    ]
    install_command = " && ".join(steps)
    result = host.execute_idempotent_command(install_command, timeout_seconds=300.0)
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
    claude_json_path = Path.home() / ".claude.json"
    if not claude_json_path.exists():
        return False
    try:
        config_data = json.loads(claude_json_path.read_text())
        return bool(config_data.get("primaryApiKey"))
    except (json.JSONDecodeError, OSError):
        return False


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


def _symlink_credentials(host: OnlineHostInterface, config_dir: Path) -> None:
    """Linux/fallback: symlink .credentials.json to the per-agent config dir."""
    credentials_source = Path.home() / ".claude" / ".credentials.json"
    credentials_dest = config_dir / ".credentials.json"
    if credentials_source.exists():
        host.execute_idempotent_command(
            f"ln -sf {shlex.quote(str(credentials_source))} {shlex.quote(str(credentials_dest))}",
            timeout_seconds=5.0,
        )
    else:
        logger.debug("No .credentials.json found to symlink")


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


def _get_local_host(mngr_ctx: MngrContext) -> OnlineHostInterface:
    """Get the local host instance for file operations."""
    local_host_ref = get_provider_instance(LOCAL_PROVIDER_NAME, mngr_ctx).get_host(HostName("localhost"))
    if not isinstance(local_host_ref, OnlineHostInterface):
        raise MngrError("Local host is not online")
    return local_host_ref


def _write_generated_files(
    host: OnlineHostInterface,
    config_dir: Path,
    generated_files: dict[Path, str],
    mngr_ctx: MngrContext,
) -> None:
    """Write generated config files to the per-agent config dir.

    For local hosts, writes files directly. For remote hosts, stages
    files to a local temp dir and rsyncs them in a single call.
    """
    if host.is_local:
        for relative, content in generated_files.items():
            dest = config_dir / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            host.write_text_file(dest, content)
    else:
        local_host = _get_local_host(mngr_ctx)
        with tempfile.TemporaryDirectory(prefix="mngr-claude-") as staging:
            for relative, content in generated_files.items():
                staged = Path(staging) / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_text(content)
            host.copy_directory(local_host, Path(staging), config_dir)


def _sync_user_resources(host: OnlineHostInterface, config_dir: Path, *, symlink: bool) -> None:
    """Sync user resource directories from the claude home dir into the per-agent config dir.

    Symlinks or copies skills/, agents/, commands/, plugins/ depending on the
    ``symlink`` flag. In symlink mode, plugins/ uses child-level symlinks
    (not a dir-level symlink) so that installed_plugins.json can be rewritten
    in place without modifying the source. settings.json is handled separately
    by _build_settings_json.
    """
    home_claude = Path.home() / ".claude"
    for dir_name in _CLAUDE_HOME_SYNC_DIRS:
        source = home_claude / dir_name
        if not source.exists():
            continue
        dest = config_dir / dir_name
        if not symlink:
            host.execute_idempotent_command(
                f"cp -r {shlex.quote(str(source))} {shlex.quote(str(dest))}", timeout_seconds=5.0
            )
        elif dir_name == "plugins":
            # Child-level symlinks so installed_plugins.json can be overwritten
            host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(dest))}", timeout_seconds=5.0)
            for child in source.iterdir():
                host.execute_idempotent_command(
                    f"ln -sf {shlex.quote(str(child))} {shlex.quote(str(dest / child.name))}",
                    timeout_seconds=5.0,
                )
        else:
            host.execute_idempotent_command(
                f"ln -sf {shlex.quote(str(source))} {shlex.quote(str(dest))}", timeout_seconds=5.0
            )


def _rsync_claude_home_directories(
    host: OnlineHostInterface,
    local_host: OnlineHostInterface,
    local_claude_dir: Path,
    config_dir: Path,
) -> None:
    """Transfer directory items from ~/.claude/ to a remote config dir using rsync.

    Uses a single host.copy_directory (rsync) call with include/exclude filters
    to transfer all directories (skills/, agents/, commands/, plugins/) at once.
    Individual files like settings.json are handled separately by the caller
    since they require generation/merging.
    """
    include_args: list[str] = []
    for dir_name in _CLAUDE_HOME_SYNC_DIRS:
        if not (local_claude_dir / dir_name).exists():
            continue
        include_args.extend([f"--include={dir_name}/", f"--include={dir_name}/**"])
    if not include_args:
        return
    include_args.append("--exclude=*")
    with log_span("Rsyncing claude home directories to per-agent config dir"):
        host.copy_directory(local_host, local_claude_dir, config_dir, extra_args=" ".join(include_args))


def _resolve_installed_plugins_sentinel(host: OnlineHostInterface) -> None:
    """Resolve sentinel-prefixed installPaths in the claude home plugins directory.

    Deploy images have installPath values rewritten to a sentinel prefix at
    build time (because the container's home directory isn't known then). This
    resolves them to the actual claude home path in place, so all downstream
    provisioning code can assume paths use the real claude home as the prefix.

    No-op if the file doesn't exist or doesn't contain the sentinel.
    """
    local_claude_dir = Path.home() / ".claude"
    installed_plugins_path = local_claude_dir / _INSTALLED_PLUGINS_RELATIVE_PATH
    if not installed_plugins_path.exists():
        return
    content = installed_plugins_path.read_text()
    if _INSTALLED_PLUGINS_SENTINEL_PREFIX not in content:
        return
    rewritten = _rewrite_installed_plugins_paths(content, Path(_INSTALLED_PLUGINS_SENTINEL_PREFIX), local_claude_dir)
    installed_plugins_path.write_text(rewritten)


def _load_claude_resource_script(filename: str) -> str:
    """Load a shell script from the mngr_claude resources package."""
    resource_files = importlib.resources.files(_claude_resources)
    script_path = resource_files.joinpath(filename)
    return script_path.read_text()


def _provision_background_scripts(
    host: OnlineHostInterface, agent_state_dir: Path, concurrency_group: ConcurrencyGroup
) -> None:
    """Write the background task scripts to $MNGR_AGENT_STATE_DIR/commands/.

    Provisions stream_transcript.sh, claude_background_tasks.sh,
    common_transcript.sh, and wait_for_stop_hook.sh so they can be
    launched by the agent's assemble_command or hooks at runtime.

    Note: mngr_log.sh (shared logging library) is provisioned by
    Host.provision_agent() to both host-level and agent-level commands
    directories, so we do not write it here.
    """
    commands_dir = agent_state_dir / "commands"
    host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(commands_dir))}", timeout_seconds=5.0)

    # Claude-specific scripts from this plugin's resources
    threads: list[ObservableThread] = []
    for script_name in (
        "stream_transcript.sh",
        "claude_background_tasks.sh",
        "common_transcript.sh",
        "wait_for_stop_hook.sh",
    ):
        script_content = _load_claude_resource_script(script_name)
        script_path = commands_dir / script_name
        with log_span("Writing {} to agent state dir", script_name):
            try:
                thread = concurrency_group.start_new_thread(
                    host.write_file, (script_path, script_content.encode(), "0755")
                )
            except InvalidConcurrencyGroupStateError:
                # The parent group is shutting down (e.g., another provisioning step
                # failed). Stop spawning threads and let the real error propagate.
                logger.debug("Concurrency group shutting down; aborting background script provisioning")
                return
            threads.append(thread)

    # make sure everything actually uploaded
    for thread in threads:
        thread.join(60.0)


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
    credentials_path = Path.home() / ".claude" / ".credentials.json"
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


def _check_settings_local_gitignored(host: OnlineHostInterface, repo_path: Path) -> None:
    """Verify .claude/settings.local.json is gitignored in the given repo path.

    Raises PluginMngrError if the file is not gitignored. Silently returns
    if the path is not a git repository.
    """
    settings_relative = Path(".claude") / "settings.local.json"

    is_git_repo = host.execute_idempotent_command(
        "git rev-parse --is-inside-work-tree",
        cwd=repo_path,
        timeout_seconds=5.0,
    )
    if not is_git_repo.success:
        return

    result = host.execute_idempotent_command(
        f"git check-ignore -q {shlex.quote(str(settings_relative))}",
        cwd=repo_path,
        timeout_seconds=5.0,
    )
    if not result.success:
        raise PluginMngrError(
            f".claude/settings.local.json is not gitignored in {repo_path}.\n"
            "mngr needs to write Claude hooks to this file, but it would appear as an unstaged change.\n"
            f"Add '.claude/settings.local.json' to your .gitignore and try again. (original error: {result.stderr})"
        )


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


class ClaudeAgent(BaseAgent[ClaudeAgentConfig]):
    """Agent implementation for Claude with session resumption support."""

    @classmethod
    def preflight_check(
        cls,
        source_host: OnlineHostInterface,
        source_path: Path,
        agent_options: CreateAgentOptions,
        agent_config: AgentTypeConfig,
        mngr_ctx: MngrContext,
    ) -> None:
        """Validate that .claude/settings.local.json is gitignored in the source repo.

        mngr writes readiness hooks to this file during provisioning. If it's not
        gitignored, it would appear as an unstaged change. Checking early avoids
        wasting time on host creation and work_dir setup before surfacing this error.
        """
        _check_settings_local_gitignored(source_host, source_path)

    def get_claude_config_dir(self) -> Path:
        """Return the per-agent Claude config directory path.

        This directory replaces ~/.claude/ for this agent when CLAUDE_CONFIG_DIR
        is set. Located at $MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/.
        """
        return self._get_agent_dir() / "plugin" / "claude" / "anthropic"

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Add CLAUDE_CONFIG_DIR and optionally enable common transcript emission."""
        env_vars["CLAUDE_CONFIG_DIR"] = str(self.get_claude_config_dir())
        config = self.agent_config
        if config.emit_common_transcript:
            env_vars["MNGR_EMIT_COMMON_TRANSCRIPT"] = "1"

    def get_lifecycle_state(self) -> AgentLifecycleState:
        """Get lifecycle state, accounting for Claude-specific permissions_waiting file.

        The PermissionRequest hook creates a 'permissions_waiting' file when Claude
        is blocked on a permission dialog. When present, this overrides RUNNING to
        WAITING since the agent cannot make progress without user intervention.
        """
        state = super().get_lifecycle_state()
        if state == AgentLifecycleState.RUNNING:
            if self._check_file_exists(self._get_agent_dir() / "permissions_waiting"):
                return AgentLifecycleState.WAITING
        return state

    def get_expected_process_name(self) -> str:
        """Return 'claude' as the expected process name.

        This overrides the base implementation because ClaudeAgent uses a complex
        shell command with exports and fallbacks, but the actual process is always 'claude'.
        """
        return "claude"

    def uses_paste_detection_send(self) -> bool:
        """Enable paste-detection send_message for Claude Code.

        Claude Code echoes input to the terminal and has a complex input handler
        that can misinterpret Enter as a literal newline if sent too quickly after
        the message text. The paste-detection approach waits for the pasted content
        to appear on screen before submitting.
        """
        return True

    def get_tui_ready_indicator(self) -> str | None:
        """Return Claude Code's banner text as the TUI ready indicator.

        Claude Code displays "Claude Code" in its banner when the TUI is ready.
        Waiting for this ensures we don't send input before the UI is fully rendered,
        which would cause the input to be lost or appear as raw text.
        """
        return "Claude Code"

    _DIALOG_INDICATORS: tuple[DialogIndicator, ...] = (
        TrustDialogIndicator(),
        CustomApiKeyDialogIndicator(),
        ThemeSelectionIndicator(),
        EffortCalloutIndicator(),
        CostThresholdDialogIndicator(),
    )

    def _preflight_send_message(self, tmux_target: str) -> None:
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

    def _build_background_tasks_command(self, session_name: str) -> str:
        """Build a shell command that starts the background tasks script.

        The background tasks script (provisioned to $MNGR_AGENT_STATE_DIR/commands/)
        handles both activity tracking and transcript export. It runs in the
        background while the tmux session is alive.
        """
        script_path = "$MNGR_AGENT_STATE_DIR/commands/claude_background_tasks.sh"
        return f"( {script_path} {shlex.quote(session_name)} ) &"

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Assemble command with --resume || --session-id format for session resumption.

        The command format is: 'claude --resume $SID args || claude --session-id UUID args'
        This allows users to hit 'up' and 'enter' in tmux to resume the session (--resume)
        or create it with that ID (--session-id). The resume path uses $MAIN_CLAUDE_SESSION_ID,
        resolved at runtime from the session tracking file (falling back to the agent UUID on
        first run).

        An activity updater is started in the background to keep the agent's activity
        timestamp up-to-date while the tmux session is alive.
        """
        if command_override is not None:
            base = str(command_override)
        elif self.agent_config.command is not None:
            base = str(self.agent_config.command)
        else:
            raise NoCommandDefinedError(f"No command defined for agent type '{self.agent_type}'")

        # Use the agent ID as the stable UUID for session identification
        agent_uuid = str(self.id.get_uuid())

        # Build the additional arguments (cli_args from config + agent_args from CLI)
        all_extra_args = self.agent_config.cli_args + agent_args
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
        # Use $CLAUDE_CONFIG_DIR (set in the agent's env file) to find session files
        # in the per-agent config dir rather than ~/.claude/.
        resume_cmd = f'( find "$CLAUDE_CONFIG_DIR" -name "$MAIN_CLAUDE_SESSION_ID" | grep . ) && {base} --resume "$MAIN_CLAUDE_SESSION_ID"'
        create_cmd = f"{base} --session-id {agent_uuid}"

        # Append additional args to both commands if present
        if args_str:
            resume_cmd = f"{resume_cmd} {args_str}"
            create_cmd = f"{create_cmd} {args_str}"

        # Build the environment exports
        # IS_SANDBOX is only set for remote hosts (not local)
        env_exports = f"export IS_SANDBOX=1 && {sid_export}" if not host.is_local else sid_export

        # Build the background tasks command (activity tracking + transcript export)
        session_name = f"{self.mngr_ctx.config.prefix}{self.name}"
        background_cmd = self._build_background_tasks_command(session_name)

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
        """
        if options.transfer_mode == TransferMode.GIT_WORKTREE:
            if not host.is_local:
                raise PluginMngrError(
                    "Git worktree transfer mode is not supported on remote hosts.\n"
                    "Claude trust extension requires local filesystem access. "
                    "Use --transfer=git-mirror instead."
                )

        config = self.agent_config

        # Validate dialogs for non-interactive local runs so we fail early with
        # a clear message. Skip when auto_dismiss_dialogs is True because
        # provision() will auto-dismiss all dialogs in that case.
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
            check_claude_dialogs_dismissed(get_claude_config_path(), trust_path)
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

    def _configure_readiness_hooks(self, host: OnlineHostInterface) -> None:
        """Configure Claude hooks for readiness signaling in the agent's work_dir.

        This writes hooks to .claude/settings.local.json in the agent's work_dir.
        The hooks signal when Claude is actively processing by creating/removing an
        'active' file in the agent's state directory.

        Skips if hooks already exist.
        """
        # Future improvement: use `claude --settings <path>` to load hooks from
        # outside the worktree (e.g. the agent state dir), eliminating the need
        # to write to .claude/settings.local.json and check that it's gitignored.
        settings_relative = Path(".claude") / "settings.local.json"
        settings_path = self.work_dir / settings_relative

        # Check gitignore. During create(), preflight_check already verified
        # this on the source, but this covers other code paths (e.g. mngr provision).
        _check_settings_local_gitignored(host, self.work_dir)

        hooks_config = build_readiness_hooks_config()

        # Read existing settings if present
        existing_settings: dict[str, Any] = {}
        try:
            content = host.read_text_file(settings_path)
            existing_settings = json.loads(content)
        except FileNotFoundError:
            pass

        # Merge hooks, checking for duplicates
        merged = merge_hooks_config(existing_settings, hooks_config)
        if merged is None:
            logger.debug("Readiness hooks already configured in {}", settings_path)
            return

        # Write the merged settings
        with log_span("Configuring readiness hooks in {}", settings_path):
            host.write_text_file(settings_path, json.dumps(merged, indent=2) + "\n")

    def interactively_dismiss_claude_dialogs(self, source_path: Path | None, mngr_ctx: MngrContext) -> None:
        """Ensure all known Claude startup dialogs are dismissed in the global config.

        All dialogs that could intercept tmux input must be dismissed before
        starting an agent, otherwise mngr message will break. Writes to the
        global config (~/.claude.json) to record user intent; the per-agent
        config inherits these settings.

        For auto-approve mode, silently dismisses all dialogs. For interactive
        mode, prompts the user for each undismissed dialog. For non-interactive
        mode, raises the appropriate error.

        source_path is the trusted source directory (for git-worktree/git-mirror modes).
        When None (rsync/none mode), trust is prompted for work_dir instead.
        """
        global_config_path = get_claude_config_path()
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

    def _find_git_source_path(self, concurrency_group: ConcurrencyGroup) -> Path | None:
        """Find the source repo path for the agent's work_dir, if it's a git worktree or mirror.

        Returns the parent of the git common dir (the source repo root),
        or None if work_dir is not inside a git repo.
        """
        git_common_dir = find_git_common_dir(self.work_dir, concurrency_group)
        if git_common_dir is None:
            return None
        return git_common_dir.parent

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
        source_claude_dir = Path.home() / ".claude"

        # Build runtime context
        copy_project_config_from: Path | None = None
        if host.is_local and options.transfer_mode in (TransferMode.GIT_WORKTREE, TransferMode.GIT_MIRROR):
            copy_project_config_from = self._find_git_source_path(mngr_ctx.concurrency_group)
        ctx = ProvisioningContext(is_unattended=not host.is_local, copy_project_config_from=copy_project_config_from)

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
        approve_api_key_for_claude(claude_json_data)

        settings_json = _build_settings_json(source_claude_dir, config, ctx, sync_local=config.sync_home_settings)

        generated_files: dict[Path, str] = {
            Path("settings.json"): settings_json,
            Path(".claude.json"): json.dumps(claude_json_data, indent=2) + "\n",
        }
        if config.sync_home_settings:
            installed_plugins = _generate_installed_plugins_content(source_claude_dir, config_dir)
            if installed_plugins:
                generated_files[_INSTALLED_PLUGINS_RELATIVE_PATH] = installed_plugins

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
                _rsync_claude_home_directories(host, _get_local_host(mngr_ctx), source_claude_dir, config_dir)
        if host.is_local:
            if config.convert_macos_credentials and is_macos():
                _provision_keychain_credentials(config_dir, mngr_ctx.concurrency_group)
            else:
                _symlink_credentials(host, config_dir)

        # 3. Write generated files to config_dir
        _write_generated_files(host, config_dir, generated_files, mngr_ctx)

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
        """
        # Resolve sentinel-prefixed installPaths in ~/.claude/ if present.
        # Deploy images have paths rewritten to a sentinel at build time
        # (because the container's home dir isn't known at build). Resolve
        # them to the actual ~/.claude/ path now, so all downstream code
        # can assume paths use ~/.claude/ as the prefix.
        _resolve_installed_plugins_sentinel(host)

        with mngr_ctx.concurrency_group.make_concurrency_group("claude_provisioning") as concurrency_group:
            config = self.agent_config

            # Provision background task scripts to the agent state directory
            provision_backgroun_script_thread = concurrency_group.start_new_thread(
                _provision_background_scripts, (host, self._get_agent_dir(), concurrency_group)
            )

            if host.is_local:
                # Determine the source path for trust extension
                source_path: Path | None = None
                transfer_mode = options.transfer_mode
                if transfer_mode in (TransferMode.GIT_WORKTREE, TransferMode.GIT_MIRROR):
                    source_path = self._find_git_source_path(mngr_ctx.concurrency_group)

                if config.auto_dismiss_dialogs:
                    # Auto-approve all dialogs for agents that opt into dismissal
                    auto_dismiss_claude_dialogs(get_claude_config_path(), self.work_dir)
                else:
                    # Check/prompt for all blocking dialogs
                    # source_path=None (clone/no-git) means trust is prompted for work_dir
                    self.interactively_dismiss_claude_dialogs(source_path, mngr_ctx)

            # Ensure claude is installed (and at the right version if pinned)
            if config.check_installation:
                is_installed = _check_claude_installed(host)
                if is_installed:
                    logger.debug("Claude is already installed on the host")
                    # If version is pinned, verify the installed version matches
                    if config.version is not None:
                        installed_version = _get_claude_version(host)
                        if installed_version != config.version:
                            raise PluginMngrError(
                                f"Claude version mismatch: installed version is {installed_version!r}, "
                                f"but agent config pins version {config.version!r}. "
                                "Re-install claude with the correct version or update the pinned version in your agent config."
                            )
                        logger.debug("Claude version {} matches pinned version", installed_version)
                else:
                    logger.warning("Claude is not installed on the host")
                    install_hint = _build_install_command_hint(config.version)

                    if host.is_local:
                        # For local hosts, auto-approve or prompt the user for consent
                        if mngr_ctx.is_auto_approve:
                            logger.debug("Auto-approving claude installation (--yes)")
                        elif mngr_ctx.is_interactive:
                            if _prompt_user_for_installation(config.version):
                                logger.debug("User consented to install claude locally")
                            else:
                                raise PluginMngrError(
                                    f"Claude is not installed. Please install it manually with:\n  {install_hint}"
                                )
                        else:
                            # Non-interactive mode: fail with a clear message
                            raise PluginMngrError(
                                f"Claude is not installed. Please install it manually with:\n  {install_hint}"
                            )
                    else:
                        if not mngr_ctx.config.is_remote_agent_installation_allowed:
                            raise PluginMngrError(
                                "Claude is not installed on the remote host and automatic remote installation is disabled. "
                                "Set is_remote_agent_installation_allowed = true in your mngr config to enable automatic installation, "
                                "or install Claude manually on the remote host."
                            )
                        else:
                            logger.debug("Automatic remote agent installation is enabled, proceeding")

                    # Install claude
                    logger.info("Installing claude...")
                    _install_claude(host, config.version)
                    logger.info("Claude installed successfully")

            # no matter what, *always* dismiss the cost popup, it's pointless
            acknowledge_cost_threshold(get_claude_config_path())

            # Transfer plugin data from source agent before config setup (if cloning via --from-agent).
            # This copies sessions, memory, transcript offsets, etc. The subsequent config setup
            # will overwrite identity-specific files (.claude.json, credentials) with fresh values.
            if options.source_agent_state_dir is not None:
                self._transfer_source_plugin_data(host, options.source_agent_state_dir)

            # Set up per-agent config directory (for both local and remote hosts)
            self._setup_per_agent_config_dir(host, options, mngr_ctx)

            # Configure readiness hooks (for both local and remote hosts)
            self._configure_readiness_hooks(host)

            # should be done by now, just wanted to do in parallel for latency reasons
            provision_backgroun_script_thread.join(60.0)

    def on_after_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Adopt sessions when --adopt-session is used.

        For each specified session, searches the user's Claude config directory
        by ID (or reads a .jsonl path directly), copies the containing project
        directory into the per-agent config dir, and writes the last session ID
        so --resume picks it up.
        """
        adopt_session_args: tuple[str, ...] = options.plugin_data.get("adopt_session", ())
        if not adopt_session_args:
            return

        config_dir = self.get_claude_config_dir()
        copied_project_dirs: set[str] = set()
        # Claude Code organizes sessions by encoded working directory path.
        # Place adopted sessions under the project dir matching this agent's
        # work_dir so that `claude --resume` can find them.
        dest_project_name = encode_claude_project_dir_name(self.work_dir)

        for arg in adopt_session_args:
            session_id, source_project_dir = _resolve_adopt_session(arg)
            # Deduplicate project dir copies (multiple sessions may be in the same project)
            if source_project_dir.name not in copied_project_dirs:
                dest_project_dir = config_dir / "projects" / dest_project_name
                with log_span("Adopting session {}", session_id):
                    host.copy_directory(host, source_project_dir, dest_project_dir)
                copied_project_dirs.add(source_project_dir.name)
            last_session_id = session_id

        assert last_session_id is not None, "adopt_session_args was non-empty but no session ID was set"

        # Remove the sessions-index.json copied from the source project dir.
        # It contains stale entries pointing to the source paths and project,
        # and its presence prevents Claude Code from discovering the adopted
        # session files. Claude Code rebuilds the index on next startup.
        dest_project_dir = config_dir / "projects" / dest_project_name
        stale_index = dest_project_dir / "sessions-index.json"
        host.execute_idempotent_command(f"rm -f {shlex.quote(str(stale_index))}")

        host.write_text_file(self._get_agent_dir() / "claude_session_id", last_session_id)
        logger.info("Adopted {} session(s), active session: {}", len(adopt_session_args), last_session_id)

    def _transfer_source_plugin_data(
        self,
        host: OnlineHostInterface,
        source_agent_state_dir: Path,
    ) -> None:
        """Transfer plugin data from a source agent's state directory during clone.

        Copies the source agent's plugin/ directory into this agent's state
        directory. This runs before _setup_per_agent_config_dir, which will
        overwrite identity-specific config files with fresh values for the
        new agent.
        """
        source_plugin_dir = source_agent_state_dir / "plugin"
        dest_plugin_dir = self._get_agent_dir() / "plugin"

        if not source_plugin_dir.exists():
            logger.debug("No plugin directory in source agent, skipping clone transfer")
            return

        with log_span("Transferring source plugin data"):
            host.copy_directory(host, source_plugin_dir, dest_plugin_dir)

    def on_destroy(self, host: OnlineHostInterface) -> None:
        """Clean up per-agent credentials and trust entries.

        For agents with per-agent config dirs: cleans up macOS keychain entries
        (the config dir itself is deleted with the agent state).
        For legacy agents without per-agent config dirs: cleans up the global
        ~/.claude.json trust entry.
        """
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
            removed = remove_claude_trust_for_path(get_claude_config_path(), self.work_dir)
            if removed:
                logger.debug("Removed Claude trust entry for {} from global config", self.work_dir)
        else:
            # Per-agent config dir on non-macOS: config dir is deleted with agent state, nothing extra to clean up
            pass


def _generate_claude_home_settings() -> dict[str, Any]:
    """default contents for ~/.claude/settings.json"""
    return {"skipDangerousModePermissionPrompt": True}


def _generate_claude_json(version: str | None, current_time: datetime | None = None) -> dict[str, Any]:
    """default contents for .claude.json"""
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
        "autoUpdates": False,
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


class WaitingReason(UpperCaseStrEnum):
    """Why a Claude agent is in the WAITING lifecycle state."""

    PERMISSIONS = auto()
    END_OF_TURN = auto()


def _host_file_exists(host: OnlineHostInterface, path: Path) -> bool:
    """Check if a file exists on the host without SSH overhead."""
    try:
        host.read_text_file(path)
        return True
    except FileNotFoundError:
        return False


def _waiting_reason(agent: AgentInterface, host: OnlineHostInterface) -> WaitingReason | None:
    """Return why the agent is waiting based on marker files, or None.

    Checks the agent state directory for marker files rather than calling
    get_lifecycle_state() (which involves tmux/ps SSH commands).

    - permissions_waiting exists -> PERMISSIONS (blocked on permission dialog)
    - active file absent -> END_OF_TURN (idle, turn complete)
    - otherwise -> None (agent is actively running)
    """
    agent_dir = host.host_dir / "agents" / str(agent.id)
    if _host_file_exists(host, agent_dir / "permissions_waiting"):
        return WaitingReason.PERMISSIONS
    if not _host_file_exists(host, agent_dir / "active"):
        return WaitingReason.END_OF_TURN
    return None


@hookimpl
def agent_field_generators() -> tuple[str, dict[str, Callable[[AgentInterface, OnlineHostInterface], Any]]] | None:
    """Expose Claude-specific agent fields for listing."""
    return ("claude", {"waiting_reason": _waiting_reason})


@hookimpl
def register_cli_options(command_name: str) -> Mapping[str, list[OptionStackItem]] | None:
    """Register the --adopt-session CLI option for the create command."""
    if command_name == "create":
        return {
            "Behavior": [
                OptionStackItem(
                    param_decls=("--adopt-session",),
                    multiple=True,
                    help="Adopt an existing Claude Code session into this agent. "
                    "Accepts a session ID or a path to a .jsonl file [repeatable].",
                ),
            ]
        }
    return None


@hookimpl
def on_before_create(args: OnBeforeCreateArgs) -> OnBeforeCreateArgs | None:
    """Validate create args when --adopt-session is used.

    When plugin_data contains "adopt_session":
    - Validates the agent type is claude (or unset/default)
    """
    adopt_session = args.agent_options.plugin_data.get("adopt_session", ())
    if not adopt_session:
        return None

    agent_type = args.agent_options.agent_type
    if agent_type is not None and str(agent_type) != "claude":
        raise UserInputError(f"--adopt-session can only be used with the claude agent type, not '{agent_type}'.")

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
    When include_user_settings is True, also includes skills/, agents/,
    commands/, and credentials.
    """
    files: dict[Path, Path | str] = {}

    local_claude_dir = Path.home() / ".claude"
    deploy_ctx = ProvisioningContext(is_unattended=True, copy_project_config_from=None)
    deploy_config = ClaudeAgentConfig()

    # settings.json always ships (generated, not a direct copy)
    files[Path("~/.claude/settings.json")] = _build_settings_json(
        local_claude_dir, deploy_config, deploy_ctx, sync_local=include_user_settings
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


def approve_api_key_for_claude(data: dict[str, Any]):
    """Approve the API key so that the agent doesn't get blocked by the custom API key dialog."""
    user_config = read_claude_config(get_claude_config_path())
    conf_key = user_config.get("primaryApiKey", "")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key or conf_key:
        approved_section = data.setdefault("customApiKeyResponses", {})
        approved_list = approved_section.get("approved", [])
        if api_key[-20:] not in approved_list:
            approved_list.append(api_key[-20:])
        if conf_key[-20:] not in approved_list:
            approved_list.append(conf_key[-20:])
        approved_section["approved"] = approved_list
        approved_section["rejected"] = []
