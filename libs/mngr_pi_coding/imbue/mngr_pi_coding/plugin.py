import json
import os
import shlex
from collections.abc import Sequence
from pathlib import Path

from loguru import logger
from pydantic import Field

from imbue.mngr import hookimpl
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.agents.tui_utils import send_enter_best_effort
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import FileTransferSpec
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString

_PI_HOME_DIR_NAME: str = ".pi"
_PI_AGENT_SUBDIR: str = "agent"


def _get_pi_home_dir(home_dir: Path | None = None) -> Path:
    """Return the pi agent home directory (defaults to ~/.pi/agent/)."""
    if home_dir is None:
        home_dir = Path.home()
    return home_dir / _PI_HOME_DIR_NAME / _PI_AGENT_SUBDIR


class PiCodingAgentConfig(AgentTypeConfig):
    """Config for the pi-coding agent type."""

    command: CommandString = Field(
        default=CommandString("pi"),
        description="Command to run the pi coding agent",
    )
    sync_home_settings: bool = Field(
        default=True,
        description="Whether to sync settings from ~/.pi/agent/ to the per-agent config dir",
    )
    sync_auth: bool = Field(
        default=True,
        description="Whether to sync the auth.json from ~/.pi/agent/ to the per-agent config dir",
    )
    check_installation: bool = Field(
        default=True,
        description="Check if pi is installed (if False, assumes it is already present)",
    )


def _check_pi_installed(host: OnlineHostInterface) -> bool:
    """Check if pi is installed on the host."""
    result = host.execute_idempotent_command("command -v pi", timeout_seconds=10.0)
    return result.success


def _install_pi(host: OnlineHostInterface) -> None:
    """Install pi on the host via npm."""
    result = host.execute_idempotent_command(
        "npm install -g @mariozechner/pi-coding-agent",
        timeout_seconds=300.0,
    )
    if not result.success:
        raise PluginMngrError(f"Failed to install pi. stderr: {result.stderr}")


def _has_api_credentials_available(
    host: OnlineHostInterface,
    options: CreateAgentOptions,
    home_dir: Path | None = None,
) -> bool:
    """Check whether API credentials appear to be available for pi.

    Pi supports many providers, but the most common is ANTHROPIC_API_KEY.
    Checks environment variables (process env for local hosts, agent env vars,
    host env vars), and the auth.json file.
    """
    api_key_env_vars = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "OPENROUTER_API_KEY",
    )

    for key in api_key_env_vars:
        if host.is_local and os.environ.get(key):
            return True
        for env_var in options.environment.env_vars:
            if env_var.key == key:
                return True
        if host.get_env_var(key):
            return True

    auth_path = _get_pi_home_dir(home_dir) / "auth.json"
    if auth_path.exists():
        # This is a best-effort heuristic that only drives a warning, so a corrupt or
        # unreadable auth.json must not abort provisioning -- treat it as "no credentials".
        try:
            auth_data = json.loads(auth_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not read pi auth.json at {}: {}", auth_path, e)
            return False
        # A present-but-empty auth.json ({}) legitimately means no credentials.
        if auth_data:
            return True

    return False


class PiCodingAgent(InteractiveTuiAgent[PiCodingAgentConfig]):
    """Agent implementation for the pi coding agent with TUI handling."""

    # Pi displays "pi v" followed by the version in its startup banner; this
    # substring stays in the visible pane for the lifetime of the session,
    # so it serves as the startup ready indicator.
    TUI_READY_INDICATOR = "pi v"

    def _send_enter_and_validate(self, tmux_target: TmuxWindowTarget) -> None:
        # Pi has no UserPromptSubmit hook and no input-row placeholder that
        # disappears during typing, so submission can't be confirmed
        # post-Enter. The earlier paste-visibility check is what gives us
        # confidence the message landed.
        send_enter_best_effort(self, tmux_target)

    def get_pi_config_dir(self) -> Path:
        """Return the per-agent pi config directory path.

        This directory replaces ~/.pi/agent/ for this agent when PI_CODING_AGENT_DIR
        is set. Located at $MNGR_AGENT_STATE_DIR/plugin/pi_coding/.
        """
        return self._get_agent_dir() / "plugin" / "pi_coding"

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Set PI_CODING_AGENT_DIR to isolate pi's config per-agent."""
        env_vars["PI_CODING_AGENT_DIR"] = str(self.get_pi_config_dir())

    def get_expected_process_name(self) -> str:
        """Return 'pi' as the expected process name.

        Pi sets process.title = "pi" in cli.ts.
        """
        return "pi"

    def on_before_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Validate preconditions before provisioning."""
        if not _has_api_credentials_available(host, options):
            logger.warning(
                "No API credentials detected for pi. The agent may fail to start.\n"
                "Provide credentials via one of:\n"
                "  - Set ANTHROPIC_API_KEY environment variable (use --pass-env ANTHROPIC_API_KEY)\n"
                "  - Run 'pi' and use /login to configure credentials in ~/.pi/agent/auth.json"
            )

    def get_provision_file_transfers(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> Sequence[FileTransferSpec]:
        """No file transfers needed -- provisioning handles config setup directly."""
        return []

    def _setup_per_agent_config_dir(
        self,
        host: OnlineHostInterface,
        config: PiCodingAgentConfig,
        home_dir: Path | None = None,
    ) -> None:
        """Create and populate the per-agent pi config directory.

        This directory is pointed to by PI_CODING_AGENT_DIR so that pi
        uses per-agent config/sessions/state instead of the global ~/.pi/agent/.
        """
        config_dir = self.get_pi_config_dir()

        result = host.execute_idempotent_command(
            f"mkdir -p -m 0700 {shlex.quote(str(config_dir))}", timeout_seconds=5.0
        )
        if not result.success:
            raise PluginMngrError(f"Failed to create per-agent config dir {config_dir}: {result.stderr}")

        if host.is_local:
            self._setup_local_config_dir(host, config, config_dir, home_dir)
        else:
            self._setup_remote_config_dir(host, config, config_dir, home_dir)

    def _setup_local_config_dir(
        self,
        host: OnlineHostInterface,
        config: PiCodingAgentConfig,
        config_dir: Path,
        home_dir: Path | None = None,
    ) -> None:
        """Set up the per-agent config dir on a local host via symlinks."""
        home_pi = _get_pi_home_dir(home_dir)

        # Resource sync is best-effort: unlike the mkdir in _setup_per_agent_config_dir
        # (which raises), an individual symlink failure here only warns and continues. Pi
        # credential/settings sync is advisory -- on_before_provisioning already surfaces
        # the no-credentials case -- so a failed symlink should not abort provisioning.
        if config.sync_auth:
            auth_source = home_pi / "auth.json"
            if auth_source.exists():
                result = host.execute_idempotent_command(
                    f"ln -sf {shlex.quote(str(auth_source))} {shlex.quote(str(config_dir / 'auth.json'))}",
                    timeout_seconds=5.0,
                )
                if not result.success:
                    logger.warning("Failed to symlink auth.json: {}", result.stderr)

        if config.sync_home_settings:
            settings_source = home_pi / "settings.json"
            if settings_source.exists():
                result = host.execute_idempotent_command(
                    f"ln -sf {shlex.quote(str(settings_source))} {shlex.quote(str(config_dir / 'settings.json'))}",
                    timeout_seconds=5.0,
                )
                if not result.success:
                    logger.warning("Failed to symlink settings.json: {}", result.stderr)

            for dir_name in ("skills", "prompts", "extensions", "themes"):
                source = home_pi / dir_name
                # These resources are expected to be directories; skip non-dir entries to
                # stay consistent with the remote path (_setup_remote_config_dir).
                if source.exists() and source.is_dir():
                    result = host.execute_idempotent_command(
                        f"ln -sf {shlex.quote(str(source))} {shlex.quote(str(config_dir / dir_name))}",
                        timeout_seconds=5.0,
                    )
                    if not result.success:
                        logger.warning("Failed to symlink {}: {}", dir_name, result.stderr)

    def _setup_remote_config_dir(
        self,
        host: OnlineHostInterface,
        config: PiCodingAgentConfig,
        config_dir: Path,
        home_dir: Path | None = None,
    ) -> None:
        """Set up the per-agent config dir on a remote host via file copies."""
        home_pi = _get_pi_home_dir(home_dir)

        if config.sync_auth:
            auth_source = home_pi / "auth.json"
            if auth_source.exists():
                logger.info("Transferring auth.json to per-agent config dir...")
                host.write_text_file(config_dir / "auth.json", auth_source.read_text())

        if config.sync_home_settings:
            settings_source = home_pi / "settings.json"
            if settings_source.exists():
                logger.info("Transferring settings.json to per-agent config dir...")
                host.write_text_file(config_dir / "settings.json", settings_source.read_text())

            # Transfer the resource directories with a single rsync rather than
            # one write_file per file. A per-file upload opens an SFTP channel
            # per file (a full round-trip over the SSH tunnel) and does not
            # scale to large resource sets -- see github issue 1825.
            include_args: list[str] = []
            for dir_name in ("skills", "prompts", "extensions", "themes"):
                if (home_pi / dir_name).is_dir():
                    include_args.extend([f"--include={dir_name}/", f"--include={dir_name}/**"])
            if include_args:
                include_args.append("--exclude=*")
                host.copy_local_directory(home_pi, config_dir, " ".join(include_args))

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Provision the per-agent config dir and install pi if needed."""
        config = self.agent_config

        if config.check_installation:
            is_installed = _check_pi_installed(host)
            if is_installed:
                logger.debug("pi is already installed on the host")
            else:
                install_hint = "npm install -g @mariozechner/pi-coding-agent"
                if host.is_local and not mngr_ctx.is_auto_approve:
                    raise PluginMngrError(f"pi is not installed. Please install it with:\n  {install_hint}")
                elif not host.is_local and not mngr_ctx.config.is_remote_agent_installation_allowed:
                    raise PluginMngrError(
                        "pi is not installed on the remote host and automatic remote installation is disabled."
                    )
                else:
                    logger.info("Installing pi...")
                    _install_pi(host)
                    logger.info("pi installed successfully")

        self._setup_per_agent_config_dir(host, config)

    def on_after_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """No post-provisioning steps needed."""

    def on_destroy(self, host: OnlineHostInterface) -> None:
        """No extra cleanup needed -- the per-agent config dir is deleted with the agent state."""


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the pi-coding agent type."""
    return ("pi-coding", PiCodingAgent, PiCodingAgentConfig)
