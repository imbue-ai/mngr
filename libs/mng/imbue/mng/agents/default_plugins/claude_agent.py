from __future__ import annotations

import json
import os
import random
import shlex
from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Final

import click
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mng import hookimpl
from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.agents.default_plugins.claude_config import ClaudeDirectoryNotTrustedError
from imbue.mng.agents.default_plugins.claude_config import ClaudeEffortCalloutNotDismissedError
from imbue.mng.agents.default_plugins.claude_config import add_claude_trust_for_path
from imbue.mng.agents.default_plugins.claude_config import build_readiness_hooks_config
from imbue.mng.agents.default_plugins.claude_config import check_claude_dialogs_dismissed
from imbue.mng.agents.default_plugins.claude_config import dismiss_effort_callout
from imbue.mng.agents.default_plugins.claude_config import ensure_claude_dialogs_dismissed
from imbue.mng.agents.default_plugins.claude_config import extend_claude_trust_to_worktree
from imbue.mng.agents.default_plugins.claude_config import is_effort_callout_dismissed
from imbue.mng.agents.default_plugins.claude_config import is_source_directory_trusted
from imbue.mng.agents.default_plugins.claude_config import merge_hooks_config
from imbue.mng.agents.default_plugins.claude_config import remove_claude_trust_for_path
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import AgentStartError
from imbue.mng.errors import NoCommandDefinedError
from imbue.mng.errors import PluginMngError
from imbue.mng.errors import SendMessageError
from imbue.mng.errors import UserInputError
from imbue.mng.hosts.common import is_macos
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.data_types import FileTransferSpec
from imbue.mng.interfaces.data_types import RelativePath
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import WorkDirCopyMode
from imbue.mng.providers.ssh_host_setup import load_resource_script
from imbue.mng.utils.git_utils import find_git_common_dir
from imbue.mng.utils.polling import poll_until

_READY_SIGNAL_TIMEOUT_SECONDS: Final[float] = 10.0

# Paths within ~/.claude/ to sync to remote hosts for Claude Code operation.
# Used by both get_files_for_deploy() and provision() to ensure consistency.
_CLAUDE_HOME_SYNC_ITEMS: Final[tuple[str, ...]] = (
    "settings.json",
    "skills",
    "agents",
    "commands",
)


class ClaudeAgentConfig(AgentTypeConfig):
    """Config for the claude agent type."""

    command: CommandString = Field(
        default=CommandString("claude"),
        description="Command to run claude agent",
    )
    sync_home_settings: bool = Field(
        default=True,
        description="Whether to sync Claude settings from ~/.claude/ to a remote host",
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
        description="Whether to sync the local ~/.claude/.credentials.json to a remote host",
    )
    override_settings_folder: Path | None = Field(
        default=None,
        description="Extra folder to sync to the repo .claude/ folder in the agent work_dir."
        "(files are transferred after user settings, so they can override)",
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


def _collect_claude_home_dir_files(claude_dir: Path) -> dict[Path, Path]:
    """Collect files from ~/.claude/ directory items for deployment.

    Returns dict mapping deployment destination paths (starting with "~/.claude/")
    to local source paths. Iterates over _CLAUDE_HOME_SYNC_ITEMS to collect files
    from both regular files and directories (recursively).
    """
    files: dict[Path, Path] = {}
    for item_name in _CLAUDE_HOME_SYNC_ITEMS:
        item_path = claude_dir / item_name
        if not item_path.exists():
            continue
        if item_path.is_dir():
            for file_path in item_path.rglob("*"):
                if file_path.is_file():
                    relative = file_path.relative_to(claude_dir)
                    files[Path(f"~/.claude/{relative}")] = file_path
        else:
            files[Path(f"~/.claude/{item_name}")] = item_path
    return files


def _build_settings_json_content(sync_local: bool) -> str:
    """Build ~/.claude/settings.json content for remote deployment.

    Uses the local file as a base when sync_local is True and the file exists,
    otherwise uses generated defaults. Always forces skipDangerousModePermissionPrompt=True.
    """
    local_path = Path.home() / ".claude" / "settings.json"
    if sync_local and local_path.exists():
        data: dict[str, Any] = json.loads(local_path.read_text())
        if data.get("fastMode") is True:
            logger.warning("Disabling fast mode for remote deployment because it is not yet supported via the API")
            data["fastMode"] = False
    else:
        data = _generate_claude_home_settings()
    data["skipDangerousModePermissionPrompt"] = True
    return json.dumps(data, indent=2) + "\n"


def _build_claude_json_for_remote(
    sync_local: bool, work_dir: Path, version: str | None, current_time: datetime | None = None
) -> dict[str, Any]:
    """Build ~/.claude.json data for remote deployment.

    Uses the local file as a base when sync_local is True and the file exists,
    otherwise uses generated defaults. Always sets dialog-suppression fields
    (bypassPermissionsModeAccepted and effortCalloutDismissed) to prevent
    startup dialogs from intercepting automated input via tmux send-keys.

    Returns the dict so callers can do further modifications (e.g. keychain merge)
    before serializing.
    """
    local_path = Path.home() / ".claude.json"
    if sync_local and local_path.exists():
        data: dict[str, Any] = json.loads(local_path.read_text())
    else:
        data = _generate_claude_json(version, current_time=current_time)
    data["bypassPermissionsModeAccepted"] = True
    data["effortCalloutDismissed"] = True
    # Add trust for the remote work_dir so Claude doesn't show the
    # trust dialog (which would intercept tmux send-keys input):
    projects = data.setdefault("projects", {})
    projects.setdefault(str(work_dir), {})["hasTrustDialogAccepted"] = True
    return data


def _check_claude_installed(host: OnlineHostInterface) -> bool:
    """Check if claude is installed on the host."""
    result = host.execute_command("command -v claude", timeout_seconds=10.0)
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
    result = host.execute_command("claude --version", timeout_seconds=10.0)
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


def _install_claude(host: OnlineHostInterface, version: str | None = None) -> None:
    """Install claude on the host using the official installer.

    When version is specified, passes it to the install script to install that
    specific version (e.g., 'bash -s 2.1.50').
    """
    if version:
        version_arg = f" -s {shlex.quote(version)}"
    else:
        version_arg = ""
    install_command = f"""curl --version && ( curl -fsSL https://claude.ai/install.sh | bash{version_arg} ) && echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc"""
    result = host.execute_command(install_command, timeout_seconds=300.0)
    if not result.success:
        raise PluginMngError(f"Failed to install claude. stderr: {result.stderr}")


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
        "mng needs to add a trust entry for this directory to ~/.claude.json\n"
        "so that Claude Code can start without showing a trust dialog.\n",
        source_path,
    )
    return click.confirm("Would you like to update ~/.claude.json to trust this directory?", default=False)


def _prompt_user_for_effort_callout_dismissal() -> bool:
    """Prompt the user to dismiss the Claude Code effort callout."""
    logger.info(
        "\nClaude Code wants you to know that you can set model effort with /model.\n"
        "mng needs to dismiss this callout in ~/.claude.json so that Claude Code\n"
        "can start without it interfering with automated input.\n",
    )
    return click.confirm("Would you like to update ~/.claude.json to dismiss this?", default=False)


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


def _provision_background_scripts(host: OnlineHostInterface) -> None:
    """Write the background task scripts to $MNG_HOST_DIR/commands/.

    Provisions export_transcript.sh and claude_background_tasks.sh so they
    can be launched by the agent's assemble_command at runtime.
    """
    commands_dir = host.host_dir / "commands"
    host.execute_command(f"mkdir -p {shlex.quote(str(commands_dir))}", timeout_seconds=5.0)

    for script_name in ("export_transcript.sh", "claude_background_tasks.sh"):
        script_content = load_resource_script(script_name)
        script_path = commands_dir / script_name
        with log_span("Writing {} to host", script_name):
            host.write_file(script_path, script_content.encode(), mode="0755")


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


class DialogIndicator(FrozenModel, ABC):
    """Base class for dialog indicators that can block agent input."""

    @abstractmethod
    def get_match_string(self) -> str:
        """Return the string to look for in the tmux pane content."""
        ...

    @abstractmethod
    def get_description(self) -> str:
        """Return a human-readable description for error messages."""
        ...


class DialogDetectedError(SendMessageError):
    """A dialog is blocking the agent's input in the terminal."""

    def __init__(self, agent_name: str, dialog_description: str) -> None:
        self.dialog_description = dialog_description
        super().__init__(
            agent_name,
            f"A dialog is blocking the agent's input ({dialog_description} detected in terminal). "
            f"Connect to the agent with 'mng connect {agent_name}' to resolve it.",
        )


class PermissionDialogIndicator(DialogIndicator):
    """Detects Claude Code permission dialogs (e.g., tool approval prompts)."""

    def get_match_string(self) -> str:
        return "Do you want to proceed?"

    def get_description(self) -> str:
        return "permission dialog"


class TrustDialogIndicator(DialogIndicator):
    """Detects the Claude Code workspace trust dialog shown on first launch in a directory."""

    def get_match_string(self) -> str:
        return "Yes, I trust this folder"

    def get_description(self) -> str:
        return "trust dialog"


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


class ClaudeAgent(BaseAgent):
    """Agent implementation for Claude with session resumption support."""

    def _get_claude_config(self) -> ClaudeAgentConfig:
        """Get the claude-specific config from this agent."""
        if isinstance(self.agent_config, ClaudeAgentConfig):
            return self.agent_config
        # Fall back to default config if not a ClaudeAgentConfig
        return ClaudeAgentConfig()

    def get_expected_process_name(self) -> str:
        """Return 'claude' as the expected process name.

        This overrides the base implementation because ClaudeAgent uses a complex
        shell command with exports and fallbacks, but the actual process is always 'claude'.
        """
        return "claude"

    def uses_marker_based_send_message(self) -> bool:
        """Enable marker-based send_message for Claude Code.

        Claude Code echoes input to the terminal and has a complex input handler
        that can misinterpret Enter as a literal newline if sent too quickly after
        the message text. The marker-based approach ensures the input handler has
        fully processed the message before submitting.
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
        PermissionDialogIndicator(),
        TrustDialogIndicator(),
        ThemeSelectionIndicator(),
        EffortCalloutIndicator(),
    )

    def _preflight_send_message(self, session_name: str) -> None:
        """Check for blocking dialogs before sending a message.

        Captures the tmux pane and checks for known dialog indicators
        (permission prompts, trust dialogs, theme selection, effort callout).
        Raises DialogDetectedError if any are found.
        """
        content = self._capture_pane_content(session_name)
        if content is None:
            return

        for indicator in self._DIALOG_INDICATORS:
            match_string = indicator.get_match_string()
            if match_string in content:
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

        The background tasks script (provisioned to $MNG_HOST_DIR/commands/)
        handles both activity tracking and transcript export. It runs in the
        background while the tmux session is alive.
        """
        script_path = "$MNG_HOST_DIR/commands/claude_background_tasks.sh"
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
            f'_MNG_READ_SID=$(cat "$MNG_AGENT_STATE_DIR/claude_session_id" 2>/dev/null || true);'
            f' export MAIN_CLAUDE_SESSION_ID="${{_MNG_READ_SID:-{agent_uuid}}}"'
        )

        # Build both command variants using the dynamic session ID
        resume_cmd = f'( find ~/.claude/ -name "$MAIN_CLAUDE_SESSION_ID" | grep . ) && {base} --resume "$MAIN_CLAUDE_SESSION_ID"'
        create_cmd = f"{base} --session-id {agent_uuid}"

        # Append additional args to both commands if present
        if args_str:
            resume_cmd = f"{resume_cmd} {args_str}"
            create_cmd = f"{create_cmd} {args_str}"

        # Build the environment exports
        # IS_SANDBOX is only set for remote hosts (not local)
        env_exports = f"export IS_SANDBOX=1 && {sid_export}" if not host.is_local else sid_export

        # Build the background tasks command (activity tracking + transcript export)
        session_name = f"{self.mng_ctx.config.prefix}{self.name}"
        background_cmd = self._build_background_tasks_command(session_name)

        # Combine: start background tasks, export env (including session ID), then run the main command (and make sure we get rid of the session started marker on each run so that wait_for_ready_signal works correctly for both new and resumed sessions)
        return CommandString(
            f"{background_cmd} {env_exports} && rm -rf $MNG_AGENT_STATE_DIR/session_started && ( {resume_cmd} ) || {create_cmd}"
        )

    def on_before_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mng_ctx: MngContext,
    ) -> None:
        """Validate preconditions before provisioning (read-only).

        This method performs read-only validation only. No writes to
        disk or interactive prompts -- actual setup happens in provision().

        For worktree mode on non-interactive runs: validates that all
        known Claude startup dialogs (trust, effort callout) are dismissed
        so we fail early with a clear message. Interactive and auto-approve
        runs skip these checks because provision() will handle them.
        """
        if options.git and options.git.copy_mode == WorkDirCopyMode.WORKTREE:
            if not host.is_local:
                raise PluginMngError(
                    "Worktree mode is not supported on remote hosts.\n"
                    "Claude trust extension requires local filesystem access. "
                    "Use --copy or --clone instead."
                )
            if not mng_ctx.is_interactive and not mng_ctx.is_auto_approve:
                git_common_dir = find_git_common_dir(self.work_dir, mng_ctx.concurrency_group)
                if git_common_dir is not None:
                    source_path = git_common_dir.parent
                    check_claude_dialogs_dismissed(source_path)

        config = self._get_claude_config()
        if not config.check_installation:
            logger.debug("Skipped claude installation check (check_installation=False)")
            return

        if not _has_api_credentials_available(host, options, config, mng_ctx.concurrency_group):
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
        mng_ctx: MngContext,
    ) -> Sequence[FileTransferSpec]:
        """Return file transfers for claude settings."""
        config = self._get_claude_config()
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

        # Only check gitignore if git is available and this is a git repository
        is_git_repo = host.execute_command(
            "git rev-parse --is-inside-work-tree",
            cwd=self.work_dir,
            timeout_seconds=5.0,
        )
        if is_git_repo.success:
            # Verify .claude/settings.local.json is gitignored to avoid unstaged changes
            result = host.execute_command(
                f"git check-ignore -q {shlex.quote(str(settings_relative))}",
                cwd=self.work_dir,
                timeout_seconds=5.0,
            )
            if not result.success:
                raise PluginMngError(
                    f".claude/settings.local.json is not gitignored in {self.work_dir}.\n"
                    "mng needs to write Claude hooks to this file, but it would appear as an unstaged change.\n"
                    f"Add '.claude/settings.local.json' to your .gitignore and try again. (original error: {result.stderr})"
                )

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

    def _ensure_no_blocking_dialogs(self, source_path: Path, mng_ctx: MngContext) -> None:
        """Ensure all known Claude startup dialogs are dismissed for source_path.

        For auto-approve mode, silently dismisses all dialogs. For interactive
        mode, prompts the user for each undismissed dialog. For non-interactive
        mode, raises the appropriate error.
        """
        if mng_ctx.is_auto_approve:
            ensure_claude_dialogs_dismissed(source_path)
            return

        if not is_source_directory_trusted(source_path):
            if not mng_ctx.is_interactive or not _prompt_user_for_trust(source_path):
                raise ClaudeDirectoryNotTrustedError(str(source_path))
            add_claude_trust_for_path(source_path)

        if not is_effort_callout_dismissed():
            if not mng_ctx.is_interactive or not _prompt_user_for_effort_callout_dismissal():
                raise ClaudeEffortCalloutNotDismissedError()
            dismiss_effort_callout()

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mng_ctx: MngContext,
    ) -> None:
        """Extend trust for worktrees and install Claude if needed.

        For worktree-mode agents, ensures all Claude startup dialogs are
        dismissed and extends trust to the worktree.

        When trust_working_directory is enabled, unconditionally adds trust
        for the agent's working directory (used by changelings that run
        --in-place in their own repo directory).
        """
        if options.git and options.git.copy_mode == WorkDirCopyMode.WORKTREE:
            git_common_dir = find_git_common_dir(self.work_dir, mng_ctx.concurrency_group)
            if git_common_dir is not None:
                source_path = git_common_dir.parent
                self._ensure_no_blocking_dialogs(source_path, mng_ctx)
                extend_claude_trust_to_worktree(source_path, self.work_dir)

        config = self._get_claude_config()

        if config.trust_working_directory and host.is_local:
            ensure_claude_dialogs_dismissed(self.work_dir)

        # ensure that claude is installed (and at the right version if pinned)
        if config.check_installation:
            is_installed = _check_claude_installed(host)
            if is_installed:
                logger.debug("Claude is already installed on the host")
                # If version is pinned, verify the installed version matches
                if config.version is not None:
                    installed_version = _get_claude_version(host)
                    if installed_version != config.version:
                        raise PluginMngError(
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
                    if mng_ctx.is_auto_approve:
                        logger.debug("Auto-approving claude installation (--yes)")
                    elif mng_ctx.is_interactive:
                        if _prompt_user_for_installation(config.version):
                            logger.debug("User consented to install claude locally")
                        else:
                            raise PluginMngError(
                                f"Claude is not installed. Please install it manually with:\n  {install_hint}"
                            )
                    else:
                        # Non-interactive mode: fail with a clear message
                        raise PluginMngError(
                            f"Claude is not installed. Please install it manually with:\n  {install_hint}"
                        )
                else:
                    if not mng_ctx.config.is_remote_agent_installation_allowed:
                        raise PluginMngError(
                            "Claude is not installed on the remote host and automatic remote installation is disabled. "
                            "Set is_remote_agent_installation_allowed = true in your mng config to enable automatic installation, "
                            "or install Claude manually on the remote host."
                        )
                    else:
                        logger.debug("Automatic remote agent installation is enabled, proceeding")

                # Install claude
                logger.info("Installing claude...")
                _install_claude(host, config.version)
                logger.info("Claude installed successfully")

        # transfer files to remote hosts:
        if not host.is_local:
            # Warn about version consistency when syncing local files
            if config.sync_home_settings or config.sync_claude_json or config.sync_claude_credentials:
                _warn_about_version_consistency(config, mng_ctx.concurrency_group)

            # Always ship ~/.claude/settings.json
            host.write_text_file(
                Path(".claude/settings.json"), _build_settings_json_content(config.sync_home_settings)
            )

            # Transfer other home dir files (skills, agents, commands) if syncing is enabled
            if config.sync_home_settings:
                logger.info("Transferring claude home directory settings to remote host...")
                local_claude_dir = Path.home() / ".claude"
                for dest_path, source_path in _collect_claude_home_dir_files(local_claude_dir).items():
                    # settings.json is handled separately above
                    if dest_path == Path("~/.claude/settings.json"):
                        continue
                    # dest_path is like Path("~/.claude/skills/foo"); strip the ~/ prefix
                    # to get a path relative to the user's home directory on the remote host
                    remote_path = Path(str(dest_path).removeprefix("~/"))
                    host.write_text_file(remote_path, source_path.read_text())

            # Always ship ~/.claude.json
            claude_json_data = _build_claude_json_for_remote(config.sync_claude_json, self.work_dir, config.version)
            # If the local file lacks primaryApiKey, try the macOS keychain
            if not claude_json_data.get("primaryApiKey") and config.convert_macos_credentials and is_macos():
                keychain_api_key = _read_macos_keychain_credential("Claude Code", mng_ctx.concurrency_group)
                if keychain_api_key is not None:
                    logger.info("Merging macOS keychain API key into ~/.claude.json for remote host...")
                    claude_json_data["primaryApiKey"] = keychain_api_key
            # FIXME: this particular write must be atomic!
            #  In order to make that happen, add an is_atomic flag to the host.write_text_file method that
            #  causes the write to go to a temp file (same file path + ".tmp") and then renames it to the original
            #  That flag should, for now, default to False (for performance reasons), and be set to True at this callsite
            #  We should leave a note here as well (that claude really dislikes non-atomic writes to this file)
            host.write_text_file(Path(".claude.json"), json.dumps(claude_json_data, indent=2) + "\n")

            if config.sync_claude_credentials:
                credentials_path = Path.home() / ".claude" / ".credentials.json"
                if credentials_path.exists():
                    logger.info("Transferring ~/.claude/.credentials.json to remote host...")
                    host.write_text_file(Path(".claude/.credentials.json"), credentials_path.read_text())
                elif config.convert_macos_credentials and is_macos():
                    # No local credentials file, but keychain may have OAuth tokens
                    keychain_credentials = _read_macos_keychain_credential(
                        "Claude Code-credentials", mng_ctx.concurrency_group
                    )
                    if keychain_credentials is not None:
                        logger.info("Writing macOS keychain OAuth credentials to remote host...")
                        host.write_text_file(Path(".claude/.credentials.json"), keychain_credentials)
                    else:
                        logger.debug(
                            "Skipped ~/.claude/.credentials.json (file does not exist, no keychain credentials)"
                        )
                else:
                    logger.debug("Skipped ~/.claude/.credentials.json (file does not exist)")

        # Configure readiness hooks (for both local and remote hosts)
        self._configure_readiness_hooks(host)

        # Provision background task scripts to the host commands directory
        _provision_background_scripts(host)

    def on_destroy(self, host: OnlineHostInterface) -> None:
        """Clean up Claude trust entries for this agent's work directory."""
        removed = remove_claude_trust_for_path(self.work_dir)
        if removed:
            logger.debug("Removed Claude trust entry for {}", self.work_dir)


def _generate_claude_home_settings() -> dict[str, Any]:
    """default contents for ~/.claude/settings.json"""
    return {"skipDangerousModePermissionPrompt": True}


def _generate_claude_json(version: str | None, current_time: datetime | None = None) -> dict[str, Any]:
    """default contents for ~/.claude.json"""
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
    }


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the claude agent type."""
    return ("claude", ClaudeAgent, ClaudeAgentConfig)


@hookimpl
def get_files_for_deploy(
    mng_ctx: MngContext,
    include_user_settings: bool,
    include_project_settings: bool,
    repo_root: Path,
) -> dict[Path, Path | str]:
    """Register claude-specific files for scheduled deployments.

    Always includes ~/.claude/settings.json and ~/.claude.json (using generated
    defaults when local files are unavailable or user settings are excluded).
    When include_user_settings is True, also includes skills/, agents/,
    commands/, and credentials from the local ~/.claude/ directory.
    """
    files: dict[Path, Path | str] = {}

    local_claude_dir = Path.home() / ".claude"

    # Always ship ~/.claude/settings.json and ~/.claude.json
    files[Path("~/.claude/settings.json")] = _build_settings_json_content(include_user_settings)
    # we set the time to a constant for better caching:
    FIXED_TIME = datetime(2026, 2, 23, 3, 4, 7, tzinfo=timezone.utc)
    # it's a little silly to pass in repo_root here, but whatever, it will also get reset when we're provisioning
    claude_json_data = _build_claude_json_for_remote(False, repo_root, None, current_time=FIXED_TIME)
    # also inject our API key here, since deployed versions need it
    user_claude_json_data = _build_claude_json_for_remote(True, Path("."), None)
    api_key = user_claude_json_data.get("primaryApiKey", os.environ.get("ANTHROPIC_API_KEY", ""))
    if api_key:
        approved_keys = claude_json_data.setdefault("customApiKeyResponses", {})
        approved_keys["approved"] = [api_key[-20:]]
        approved_keys["rejected"] = []
    files[Path("~/.claude.json")] = json.dumps(claude_json_data, indent=2) + "\n"

    if include_user_settings:
        # Skills, agents, commands (skip settings.json, handled above)
        for dest_path, source_path in _collect_claude_home_dir_files(local_claude_dir).items():
            if dest_path == Path("~/.claude/settings.json"):
                continue
            files[dest_path] = source_path

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
    mng_ctx: MngContext,
    env_vars: dict[str, str],
) -> None:
    if "ANTHROPIC_API_KEY" not in env_vars:
        user_claude_json_data = _build_claude_json_for_remote(True, Path("."), None)
        token = user_claude_json_data.get("primaryApiKey", "") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not token:
            raise UserInputError(
                "ANTHROPIC_API_KEY environment variable is not set and no API key found in ~/.claude.json. "
                "You must provide credentials to authenticate with Claude Code in order for the deployment to work."
            )
        env_vars["ANTHROPIC_API_KEY"] = token
    env_vars["IS_SANDBOX"] = "1"
