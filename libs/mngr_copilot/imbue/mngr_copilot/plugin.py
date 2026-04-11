from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Sequence

from loguru import logger
from pydantic import Field

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.common import is_macos
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import FileTransferSpec
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr_copilot import hookimpl

# Keychain service name used by the Copilot CLI (via keytar).
_COPILOT_KEYCHAIN_SERVICE: str = "copilot-cli"

# Env var names checked by the Copilot CLI, in priority order.
_COPILOT_TOKEN_ENV_VARS: tuple[str, ...] = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

# npm package name for the Copilot CLI.
_COPILOT_NPM_PACKAGE: str = "@github/copilot"

# Expected process title set by the Copilot CLI.
_COPILOT_PROCESS_NAME: str = "copilot"


class CopilotAgentConfig(AgentTypeConfig):
    """Config for the copilot agent type."""

    command: CommandString = Field(
        default=CommandString("copilot"),
        description="Command used to launch the Copilot CLI.",
    )
    sync_copilot_credentials: bool = Field(
        default=True,
        description=(
            "Read GitHub credentials from the local macOS keychain (stored by 'copilot login') "
            "and inject them as COPILOT_GITHUB_TOKEN for the agent. "
            "Has no effect if any of COPILOT_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN is already set. "
            "Disable if you prefer to manage credentials entirely via env vars."
        ),
    )
    check_installation: bool = Field(
        default=True,
        description="Check if the Copilot CLI is installed on the host before provisioning.",
    )
    allow_all_tools: bool = Field(
        default=True,
        description=(
            "Pass --allow-all-tools when launching the Copilot CLI, suppressing tool-use "
            "confirmation dialogs. Recommended for automated mngr operation. "
            "Disable if you want Copilot to prompt before running shell commands."
        ),
    )


def _read_token_from_macos_keychain() -> str | None:
    """Read the first Copilot token stored by 'copilot login' from the macOS keychain.

    The Copilot CLI stores tokens via keytar with service='copilot-cli'.
    We use 'security find-generic-password' to retrieve any stored token
    without needing to know the exact account (host:login key).
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", _COPILOT_KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        logger.debug("macOS security binary not found; skipping keychain credential read")
        return None
    if result.returncode != 0:
        logger.debug("No Copilot token found in macOS keychain (service={!r})", _COPILOT_KEYCHAIN_SERVICE)
        return None
    token = result.stdout.strip()
    return token or None


def _has_token_available(
    host: OnlineHostInterface,
    options: CreateAgentOptions,
    *,
    sync_copilot_credentials: bool = True,
) -> bool:
    """Return True if a GitHub token appears to be available for the Copilot CLI.

    Checks env vars (process env on local hosts, agent options, host env) and,
    if sync_copilot_credentials is True on a macOS machine, the keychain.
    The keychain check is not gated on host.is_local because modify_env_vars
    always reads credentials from the local machine and injects them regardless
    of whether the target host is local or remote.
    """
    for key in _COPILOT_TOKEN_ENV_VARS:
        if host.is_local and os.environ.get(key):
            return True
        for env_var in options.environment.env_vars:
            if env_var.key == key:
                return True
        if host.get_env_var(key):
            return True
    # Check macOS keychain -- credentials are injected from the local machine
    # into the agent env regardless of whether the host is local or remote.
    if sync_copilot_credentials and is_macos() and _read_token_from_macos_keychain() is not None:
        return True
    return False


def _check_copilot_installed(host: OnlineHostInterface) -> bool:
    """Return True if the Copilot CLI is available on the host."""
    result = host.execute_idempotent_command("command -v copilot", timeout_seconds=10.0)
    return result.success


def _install_copilot(host: OnlineHostInterface) -> None:
    """Install the Copilot CLI on the host.

    Tries installation methods in order of preference:
    1. Official install script (curl -fsSL https://gh.io/copilot-install | bash) --
       downloads a pre-built binary, no Node.js required, works on Linux/macOS.
    2. Homebrew (brew install copilot-cli) -- if brew is available.
    3. npm (npm install -g @github/copilot) -- requires Node.js 22+.
    """
    if host.execute_idempotent_command("command -v curl", timeout_seconds=10.0).success:
        logger.info("Installing Copilot CLI via official install script...")
        result = host.execute_idempotent_command(
            "curl -fsSL https://gh.io/copilot-install | bash",
            timeout_seconds=300.0,
        )
        if result.success:
            return
        logger.debug("curl install script failed ({}); trying fallback methods", result.stderr)

    if host.execute_idempotent_command("command -v brew", timeout_seconds=10.0).success:
        logger.info("Installing Copilot CLI via Homebrew...")
        result = host.execute_idempotent_command(
            "brew install copilot-cli",
            timeout_seconds=300.0,
        )
        if result.success:
            return
        logger.debug("brew install failed ({}); trying npm", result.stderr)

    if host.execute_idempotent_command("command -v npm", timeout_seconds=10.0).success:
        logger.info("Installing Copilot CLI via npm...")
        result = host.execute_idempotent_command(
            f"npm install -g {_COPILOT_NPM_PACKAGE}",
            timeout_seconds=300.0,
        )
        if result.success:
            return
        raise PluginMngrError(f"Failed to install Copilot CLI via npm. stderr: {result.stderr}")

    raise PluginMngrError(
        "Could not install Copilot CLI: none of curl, brew, or npm are available on the host.\n"
        "Consider providing a Dockerfile with the Copilot CLI pre-installed."
    )


class CopilotAgent(BaseAgent[CopilotAgentConfig]):
    """Agent type that runs the GitHub Copilot CLI in a managed tmux session."""

    def _get_copilot_home_dir(self) -> Path:
        """Return the per-agent COPILOT_HOME directory."""
        return self.work_dir / ".copilot"

    def get_expected_process_name(self) -> str:
        """Return 'copilot' as the expected process name."""
        return _COPILOT_PROCESS_NAME

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Assemble the Copilot CLI launch command.

        Appends --allow-all-tools when allow_all_tools is True (the default),
        suppressing tool-use confirmation dialogs for automated mngr operation.
        """
        if command_override is not None:
            base = str(command_override)
        elif self.agent_config.command is not None:
            base = str(self.agent_config.command)
        else:
            base = _COPILOT_PROCESS_NAME

        extra: list[str] = []
        if self.agent_config.allow_all_tools:
            extra.append("--allow-all-tools")
        extra.extend(self.agent_config.cli_args)
        extra.extend(agent_args)

        parts = [base] + extra
        return CommandString(" ".join(parts))

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Set COPILOT_HOME for per-agent isolation.

        If sync_copilot_credentials is enabled and no token env var is already set,
        reads a token from the local macOS keychain and injects it as
        COPILOT_GITHUB_TOKEN so the remote CLI can authenticate.
        """
        env_vars["COPILOT_HOME"] = str(self._get_copilot_home_dir())

        if not self.agent_config.sync_copilot_credentials:
            return
        if any(k in env_vars for k in _COPILOT_TOKEN_ENV_VARS):
            logger.debug("Copilot token already set via env var; skipping keychain read")
            return
        if not is_macos():
            return

        token = _read_token_from_macos_keychain()
        if token is not None:
            env_vars["COPILOT_GITHUB_TOKEN"] = token
            logger.info("Injected Copilot token from macOS keychain into agent env")
        else:
            logger.warning(
                "No Copilot token found in macOS keychain. "
                "Run 'copilot login' locally, or pass a token via --pass-env COPILOT_GITHUB_TOKEN."
            )

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        config = self.agent_config

        if config.check_installation:
            if _check_copilot_installed(host):
                logger.debug("Copilot CLI is already installed on the host")
            else:
                install_hint = f"npm install -g {_COPILOT_NPM_PACKAGE}"
                if host.is_local and not mngr_ctx.is_auto_approve:
                    raise PluginMngrError(
                        f"Copilot CLI is not installed. Please install it with:\n  {install_hint}"
                    )
                elif not host.is_local and not mngr_ctx.config.is_remote_agent_installation_allowed:
                    raise PluginMngrError(
                        "Copilot CLI is not installed on the remote host and automatic "
                        "remote installation is disabled."
                    )
                else:
                    logger.info("Installing Copilot CLI...")
                    _install_copilot(host)
                    logger.info("Copilot CLI installed successfully")

        copilot_home_dir = self._get_copilot_home_dir()
        host.execute_idempotent_command(f"mkdir -p {str(copilot_home_dir)!r}", timeout_seconds=10.0)

        # Pre-trust the agent's work directory so the Copilot CLI trust dialog
        # doesn't block startup. The CLI reads trusted_folders from config.json
        # inside COPILOT_HOME before displaying the dialog.
        config_path = copilot_home_dir / "config.json"
        trust_config = {"trusted_folders": [str(self.work_dir)]}
        host.write_text_file(config_path, json.dumps(trust_config, indent=2))

    def get_provision_file_transfers(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> Sequence[FileTransferSpec]:
        return []

    def on_before_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Validate credentials are available before provisioning."""
        if not _has_token_available(
            host, options, sync_copilot_credentials=self.agent_config.sync_copilot_credentials
        ):
            logger.warning(
                "No GitHub token detected for Copilot CLI. The agent may fail to authenticate.\n"
                "Provide credentials via one of:\n"
                "  - Run 'copilot login' locally (token stored in macOS keychain)\n"
                "  - Pass a token via --pass-env COPILOT_GITHUB_TOKEN (or GH_TOKEN / GITHUB_TOKEN)"
            )

    def on_after_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        pass

    def on_destroy(self, host: OnlineHostInterface) -> None:
        pass

    def send_message(self, message: str) -> None:
        """Send a message to the Copilot CLI TUI.

        The Copilot CLI's Ink-based TUI does not echo pasted text back to the
        terminal in a way that tmux capture-pane can observe, so paste-detection
        synchronization cannot be used. Instead, we paste the text, wait briefly
        for the TUI's React event loop to process the input, then send Enter.
        """
        self._send_tmux_literal_keys(self.tmux_target, message)
        time.sleep(0.5)
        result = self.host.execute_stateful_command(
            f"tmux send-keys -t '{self.tmux_target}' Enter"
        )
        if not result.success:
            raise SendMessageError(
                str(self.name), f"tmux send-keys Enter failed: {result.stderr or result.stdout}"
            )

    def get_tui_ready_indicator(self) -> str | None:
        """Return the Copilot CLI's input prompt as the TUI ready indicator.

        The Copilot CLI renders an input prompt with a '>' character when the TUI
        is ready to accept input.
        """
        return "\u276f"


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the copilot agent type."""
    return ("copilot", CopilotAgent, CopilotAgentConfig)
