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
from concurrent.futures import Future
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
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
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
from imbue.mngr_claude.claude_config import build_readiness_hooks_config
from imbue.mngr_claude.claude_config import check_claude_dialogs_dismissed
from imbue.mngr_claude.claude_config import complete_onboarding
from imbue.mngr_claude.claude_config import dismiss_effort_callout
from imbue.mngr_claude.claude_config import encode_claude_project_dir_name
from imbue.mngr_claude.claude_config import ensure_claude_dialogs_dismissed
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
(e.g. /Users/ev/.claude/plugins/cache/...) to use this sentinel prefix.
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
    trust_working_directory: bool = Field(
        default=False,
        description="Automatically add the agent's working directory to Claude's trusted directories "
        "in ~/.claude.json before startup. This prevents the trust dialog from appearing. "
        "Also dismisses the effort callout dialog.",
    )
    model: str | None = Field(
        default=None,
        description="Model to use for this agent (e.g. 'opus[1m]'). Written to $CLAUDE_CONFIG_DIR/settings.json.",
    )
    is_fast: bool = Field(
        default=False,
        description="Whether to enable fast mode for this agent. Written to $CLAUDE_CONFIG_DIR/settings.json.",
    )
    emit_common_transcript: bool = Field(
        default=True,
        description="Emit a common, agent-agnostic transcript alongside the raw Claude transcript. "
        "When enabled, a background process converts raw transcript events into a common format at "
        "events/claude/common_transcript/events.jsonl. The common format includes user messages, "
        "assistant messages, and tool call/result summaries.",
    )


@pure
def _rewrite_installed_plugins_paths(content: str, local_claude_dir: Path, target_config_dir: Path) -> str:
    """Rewrite installPath values in installed_plugins.json for a target config dir.

    Rebases absolute local paths (under local_claude_dir) onto target_config_dir
    so that Claude Code can find plugin files in the per-agent config dir.
    """
    data: dict[str, Any] = json.loads(content)
    local_prefix = str(local_claude_dir) + "/"
    for plugin_entries in data.get("plugins", {}).values():
        for entry in plugin_entries:
            install_path = entry.get("installPath", "")
            if install_path.startswith(local_prefix):
                relative = install_path[len(local_prefix) :]
                entry["installPath"] = str(target_config_dir / relative)
    return json.dumps(data, indent=2) + "\n"


def _build_settings_json_content(
    sync_local: bool,
    model: str | None = None,
    is_fast: bool = False,
) -> str:
    """Build settings.json content for remote/deploy per-agent config dirs.

    Used for remote hosts and deploy only. Local hosts symlink settings.json
    from ~/.claude/ instead, preserving the user's exact settings.

    Uses the local file as a base when sync_local is True and the file exists,
    otherwise uses generated defaults. Forces skipDangerousModePermissionPrompt=True
    """
    local_path = Path.home() / ".claude" / "settings.json"
    if sync_local and local_path.exists():
        data: dict[str, Any] = json.loads(local_path.read_text())
    else:
        data = _generate_claude_home_settings()
    if model is not None:
        data["model"] = model
    if is_fast:
        data["fastMode"] = True
    else:
        data["fastMode"] = False
    data["skipDangerousModePermissionPrompt"] = True
    return json.dumps(data, indent=2) + "\n"


def build_claude_json_for_agent(
    sync_local: bool, work_dir: Path, version: str | None, current_time: datetime | None = None
) -> dict[str, Any]:
    """Build .claude.json data for the per-agent config dir.

    Used for remote hosts and deploys where all dialogs must be suppressed
    to prevent them from intercepting automated tmux input. Uses the local
    file as a base when sync_local is True and the file exists, otherwise
    uses generated defaults. Forces bypassPermissionsModeAccepted,
    effortCalloutDismissed, and hasAcknowledgedCostThreshold.

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
    data["bypassPermissionsModeAccepted"] = True
    data["effortCalloutDismissed"] = True
    data["hasAcknowledgedCostThreshold"] = True
    data["hasCompletedOnboarding"] = True
    # Add trust for work_dir so Claude doesn't show the trust dialog
    # (which would intercept tmux send-keys input):
    projects = data.setdefault("projects", {})
    projects.setdefault(str(work_dir), {})["hasTrustDialogAccepted"] = True
    return data


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


def _provision_file_credentials(host: OnlineHostInterface, config_dir: Path) -> None:
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


def _provision_remote_credentials(
    host: OnlineHostInterface, config_dir: Path, concurrency_group: ConcurrencyGroup, convert_macos: bool
) -> None:
    """Remote hosts: read credentials locally (files or keychain), write flat files to the remote."""
    credentials_path = Path.home() / ".claude" / ".credentials.json"
    if credentials_path.exists():
        logger.info("Transferring .credentials.json to per-agent config dir...")
        host.write_text_file(config_dir / ".credentials.json", credentials_path.read_text())
    elif convert_macos and is_macos():
        keychain_credentials = _read_macos_keychain_credential("Claude Code-credentials", concurrency_group)
        if keychain_credentials is not None:
            logger.info("Writing macOS keychain OAuth credentials to per-agent config dir...")
            host.write_text_file(config_dir / ".credentials.json", keychain_credentials)
        else:
            logger.debug("Skipped .credentials.json (file does not exist, no keychain credentials)")
    else:
        logger.debug("Skipped .credentials.json (file does not exist)")


def _provision_remote_api_key(
    host: OnlineHostInterface,
    config_dir: Path,
    claude_json_data: dict[str, Any],
    config: "ClaudeAgentConfig",
    concurrency_group: ConcurrencyGroup,
) -> None:
    """Inject primaryApiKey from the macOS keychain into the remote .claude.json if needed.

    Re-reads and rewrites .claude.json on the remote host if an API key is found
    in the keychain but wasn't in the synced local config.
    """
    if claude_json_data.get("primaryApiKey"):
        return
    if not config.convert_macos_credentials or not is_macos():
        return
    keychain_api_key = _read_macos_keychain_credential("Claude Code", concurrency_group)
    if keychain_api_key is None:
        return
    logger.info("Merging macOS keychain API key into remote per-agent .claude.json...")
    claude_json_data["primaryApiKey"] = keychain_api_key
    host.write_text_file(config_dir / ".claude.json", json.dumps(claude_json_data, indent=2) + "\n")


def _sync_local_user_resources(host: OnlineHostInterface, config_dir: Path, *, symlink: bool) -> None:
    """Sync user resource directories from ~/.claude/ into the per-agent config dir.

    Symlinks or copies skills/, agents/, commands/, plugins/ depending on the
    ``symlink`` flag. settings.json is handled separately by
    _setup_local_settings_json.
    """
    home_claude = Path.home() / ".claude"
    link_or_copy_dir = "ln -sf" if symlink else "cp -r"
    for dir_name in _CLAUDE_HOME_SYNC_DIRS:
        source = home_claude / dir_name
        if source.exists():
            host.execute_idempotent_command(
                f"{link_or_copy_dir} {shlex.quote(str(source))} {shlex.quote(str(config_dir / dir_name))}",
                timeout_seconds=5.0,
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


def _fixup_installed_plugins_json(host: OnlineHostInterface, config_dir: Path) -> None:
    """Rewrite installPath values in the per-agent installed_plugins.json.

    Called after rsync (remote) or copy (local) has placed the file in
    config_dir. Reads via host.read_text_file so it works transparently
    for both local and remote hosts.

    Handles three source path cases:
    - Deploy runtime: paths use the sentinel prefix (written by get_files_for_deploy)
    - Remote/local: paths use the current machine's ~/.claude/
    """
    installed_plugins_path = config_dir / _INSTALLED_PLUGINS_RELATIVE_PATH
    try:
        content = host.read_text_file(installed_plugins_path)
    except FileNotFoundError:
        return

    # Try sentinel prefix first (deploy case), then fall back to local ~/.claude/
    source_claude_dir: Path
    if _INSTALLED_PLUGINS_SENTINEL_PREFIX in content:
        source_claude_dir = Path(_INSTALLED_PLUGINS_SENTINEL_PREFIX)
    else:
        source_claude_dir = Path.home() / ".claude"

    rewritten = _rewrite_installed_plugins_paths(content, source_claude_dir, config_dir)
    if rewritten == content:
        return

    host.write_text_file(installed_plugins_path, rewritten)


def _setup_local_settings_json(
    host: OnlineHostInterface,
    config_dir: Path,
    config: ClaudeAgentConfig,
) -> None:
    """Set up settings.json in the per-agent config dir on a local host.

    When no overrides are needed, symlinks to ~/.claude/settings.json.
    When overrides (model, is_fast) are present, reads the user's settings
    as a base and writes a modified copy with overrides applied.
    """
    settings_path = config_dir / "settings.json"
    source = Path.home() / ".claude" / "settings.json"

    if config.model is None and not config.is_fast:
        # No overrides -- symlink to the user's settings
        if source.exists():
            host.execute_idempotent_command(
                f"ln -sf {shlex.quote(str(source))} {shlex.quote(str(settings_path))}", timeout_seconds=5.0
            )
        return

    # Overrides needed -- read source as base, apply overrides, write a regular file
    data: dict[str, Any] = {}
    if source.exists():
        try:
            data = json.loads(source.read_text())
        except json.JSONDecodeError:
            logger.warning("Corrupt settings.json, replacing with overrides only")

    if config.model is not None:
        data["model"] = config.model
    if config.is_fast:
        data["fastMode"] = True

    host.write_text_file(settings_path, json.dumps(data, indent=2) + "\n")


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
        # a clear message. Skip when trust_working_directory is True because
        # provision() will auto-dismiss all dialogs in that case.
        if (
            host.is_local
            and not mngr_ctx.is_interactive
            and not mngr_ctx.is_auto_approve
            and not config.trust_working_directory
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

    def _ensure_no_blocking_dialogs(self, source_path: Path | None, mngr_ctx: MngrContext) -> None:
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
            ensure_claude_dialogs_dismissed(global_config_path, trust_path)
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

        This directory is pointed to by CLAUDE_CONFIG_DIR so that Claude Code
        uses per-agent config/sessions/state instead of the global ~/.claude/.

        For local hosts:
        - Copies .claude.json from global config (with per-agent trust entries)
        - Symlinks .credentials.json (or copies keychain credentials on macOS)
        - Symlinks settings.json, skills/, agents/, commands/, plugins/ from ~/.claude/

        For remote hosts:
        - Writes .claude.json, .credentials.json, settings.json directly
        - Copies skills/, agents/, commands/, plugins/ from ~/.claude/
        """
        config = self.agent_config
        config_dir = self.get_claude_config_dir()

        # Create the config directory (0700: contains credentials and session data)
        host.execute_idempotent_command(f"mkdir -p -m 0700 {shlex.quote(str(config_dir))}", timeout_seconds=5.0)

        if host.is_local:
            self._setup_local_config_dir(host, options, config, config_dir)
        else:
            self._setup_remote_config_dir(host, options, config, config_dir, mngr_ctx)

    def _setup_local_config_dir(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        config: ClaudeAgentConfig,
        config_dir: Path,
    ) -> None:
        """Set up the per-agent config dir on a local host."""
        claude_json_data = self._build_per_agent_claude_json(options, config)
        approve_api_key_for_claude(claude_json_data)
        host.write_text_file(config_dir / ".claude.json", json.dumps(claude_json_data, indent=2) + "\n")

        if config.convert_macos_credentials and is_macos():
            _provision_keychain_credentials(config_dir, self.mngr_ctx.concurrency_group)
        else:
            _provision_file_credentials(host, config_dir)

        if config.sync_home_settings:
            _sync_local_user_resources(host, config_dir, symlink=config.symlink_user_resources)
            if not config.symlink_user_resources:
                _fixup_installed_plugins_json(host, config_dir)

        _setup_local_settings_json(host, config_dir, config)

    def _setup_remote_config_dir(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        config: ClaudeAgentConfig,
        config_dir: Path,
        mngr_ctx: MngrContext,
    ) -> None:
        """Set up the per-agent config dir on a remote host."""
        # Warn about version consistency when syncing local files
        if config.sync_home_settings or config.sync_claude_json or config.sync_claude_credentials:
            _warn_about_version_consistency(config, mngr_ctx.concurrency_group)

        file_transfers: list[tuple[Path, bytes]] = []
        local_claude_dir = Path.home() / ".claude"

        # 1. Always ship settings.json (generated content, not a direct copy)
        file_transfers.append(
            (
                config_dir / "settings.json",
                _build_settings_json_content(
                    config.sync_home_settings,
                    model=config.model,
                    is_fast=config.is_fast,
                ).encode("utf-8"),
            )
        )

        # 2. Rsync directory items (skills, agents, commands, plugins) in bulk,
        # then fix up installed_plugins.json paths for the remote host
        if config.sync_home_settings:
            logger.info("Transferring claude home directory settings to per-agent config dir...")
            local_host_ref = get_provider_instance(LOCAL_PROVIDER_NAME, mngr_ctx).get_host(HostName("localhost"))
            if not isinstance(local_host_ref, OnlineHostInterface):
                raise MngrError("Local host is not online")
            _rsync_claude_home_directories(host, local_host_ref, local_claude_dir, config_dir)
            _fixup_installed_plugins_json(host, config_dir)

        # 3. Always ship .claude.json (generated content, not a direct copy)
        # Resolve the work_dir on the remote host so the trust entry matches
        # the path Claude Code sees (e.g., Modal symlinks /mngr/... to /__modal/volumes/...)
        resolved_work_dir = self.work_dir
        realpath_result = host.execute_idempotent_command(
            f"realpath {shlex.quote(str(self.work_dir))}", timeout_seconds=5.0
        )
        if realpath_result.success and realpath_result.stdout.strip():
            resolved_work_dir = Path(realpath_result.stdout.strip())
        claude_json_data = build_claude_json_for_agent(config.sync_claude_json, resolved_work_dir, config.version)
        approve_api_key_for_claude(claude_json_data)
        file_transfers.append(
            (config_dir / ".claude.json", (json.dumps(claude_json_data, indent=2) + "\n").encode("utf-8"))
        )

        # Ship the generated files (settings.json, .claude.json):
        _parallel_file_transfer(file_transfers, host, mngr_ctx)

        # 4. Ship credentials (API key via .claude.json, OAuth via .credentials.json)
        _provision_remote_api_key(host, config_dir, claude_json_data, config, mngr_ctx.concurrency_group)
        if config.sync_claude_credentials:
            _provision_remote_credentials(
                host, config_dir, mngr_ctx.concurrency_group, config.convert_macos_credentials
            )

    def _build_per_agent_claude_json(
        self,
        options: CreateAgentOptions,
        config: ClaudeAgentConfig,
    ) -> dict[str, Any]:
        """Build the per-agent .claude.json for local hosts.

        Starts from the user's global ~/.claude.json to preserve all existing
        dialog states (trust, effort callout, bypass permissions, onboarding).
        Adds per-agent identity (worktree source project config, primaryApiKey)
        and forces hasAcknowledgedCostThreshold to suppress the cost threshold
        dialog. Falls back to generated defaults if no global config exists.

        Trust for work_dir is added by extending from the source directory
        (for git-worktree/git-mirror modes), by trust_working_directory config,
        or inherited from the global config (for rsync/none modes where the
        user was already prompted). Falls back to generated defaults if no global
        config exists.
        """
        global_config = read_claude_config(get_claude_config_path())
        if global_config:
            data = global_config
        else:
            data = _generate_claude_json(config.version)

        projects = data.setdefault("projects", {})
        transfer_mode = options.transfer_mode

        # For worktree/mirror mode, extend trust from the source to the work_dir
        if transfer_mode in (TransferMode.GIT_WORKTREE, TransferMode.GIT_MIRROR):
            source_path = self._find_git_source_path(self.mngr_ctx.concurrency_group)
            if source_path is not None:
                source_path = source_path.resolve()
                global_projects = global_config.get("projects", {})
                source_config = find_project_config(global_projects, source_path)
                if source_config is not None:
                    projects[str(source_path)] = source_config
                    worktree_path_str = str(self.work_dir.resolve())
                    if worktree_path_str not in projects:
                        worktree_config = copy.deepcopy(source_config)
                        worktree_config["_mngrCreated"] = True
                        worktree_config["_mngrSourcePath"] = str(source_path)
                        projects[worktree_path_str] = worktree_config

        # trust_working_directory: auto-add trust for work_dir
        if config.trust_working_directory:
            projects.setdefault(str(self.work_dir.resolve()), {})["hasTrustDialogAccepted"] = True

        # Always suppress the cost threshold dialog (it blocks all input)
        data["hasAcknowledgedCostThreshold"] = True

        return data

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
        - trust_working_directory=True: trust is auto-added for work_dir
        """
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

                if config.trust_working_directory:
                    # Auto-approve all dialogs for agents that opt into trust
                    ensure_claude_dialogs_dismissed(get_claude_config_path(), self.work_dir)
                else:
                    # Check/prompt for all blocking dialogs
                    # source_path=None (clone/no-git) means trust is prompted for work_dir
                    self._ensure_no_blocking_dialogs(source_path, mngr_ctx)

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


_MKDIR_BATCH_SIZE: Final[int] = 50


def _parallel_file_transfer(transfers: Sequence[tuple[Path, bytes]], host, mngr_ctx):
    # Deduplicate parent directories -- many files share the same parent, and
    # sending all 1000+ paths in a single mkdir command can exceed the OS
    # argument length limit ("/bin/bash: File name too long").
    unique_dirs = sorted({str(dest_path.parent) for dest_path, _ in transfers})

    # Batch mkdir calls so no single command exceeds OS argument limits.
    for i in range(0, len(unique_dirs), _MKDIR_BATCH_SIZE):
        batch = unique_dirs[i : i + _MKDIR_BATCH_SIZE]
        args = " ".join(shlex.quote(d) for d in batch)
        mkdir_result = host.execute_idempotent_command(f"mkdir -p {args}")
        if not mkdir_result.success:
            raise MngrError(f"Failed to create directories: {mkdir_result.stderr}")

    # then upload them all in parallel
    count = 0
    futures: list[Future[None]] = []
    with ConcurrencyGroupExecutor(
        parent_cg=mngr_ctx.concurrency_group, name="upload_deploy_files", max_workers=16
    ) as executor:
        for dest_path, dest_contents in transfers:
            futures.append(executor.submit(host.write_file, path=dest_path, content=dest_contents))
            logger.trace("Uploaded deploy file: {} -> {}", dest_path, dest_path)
            count += 1

    # Re-raise any thread exceptions (e.g. abort-mode errors)
    for future in futures:
        future.result()


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

    # settings.json always ships (generated, not a direct copy)
    files[Path("~/.claude/settings.json")] = _build_settings_json_content(include_user_settings)

    # Always ship .claude.json to $HOME/.claude/ in the deploy image.
    # we set the time to a constant for better caching:
    FIXED_TIME = datetime(2026, 2, 23, 3, 4, 7, tzinfo=timezone.utc)
    # it's a little silly to pass in repo_root here, but whatever, it will also get reset when we're provisioning
    claude_json_data = build_claude_json_for_agent(False, repo_root, None, current_time=FIXED_TIME)
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
                content = file_path.read_text()
                # Rewrite installPath values at build time to use the sentinel prefix,
                # so the runtime fixup doesn't need to know the build machine's home dir
                if relative_path == _INSTALLED_PLUGINS_RELATIVE_PATH:
                    content = _rewrite_installed_plugins_paths(
                        content, local_claude_dir, Path(_INSTALLED_PLUGINS_SENTINEL_PREFIX)
                    )
                files[Path("~/.claude") / relative_path] = content

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
        user_claude_json_data = build_claude_json_for_agent(True, Path("."), None)
        token = user_claude_json_data.get("primaryApiKey", "") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not token:
            raise UserInputError(
                "ANTHROPIC_API_KEY environment variable is not set and no API key found in ~/.claude.json. "
                "You must provide credentials to authenticate with Claude Code in order for the deployment to work."
            )
        env_vars["ANTHROPIC_API_KEY"] = token
    env_vars["IS_SANDBOX"] = "1"


def approve_api_key_for_claude(data: dict[str, Any]):
    # approve the API key so that the agent doesnt get blocked
    user_claude_json_data = build_claude_json_for_agent(True, Path("."), None)
    conf_key = user_claude_json_data.get("primaryApiKey", "")
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
