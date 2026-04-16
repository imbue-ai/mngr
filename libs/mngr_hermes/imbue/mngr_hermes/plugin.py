import shlex
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mngr import hookimpl
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.api.providers import get_provider_instance
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME

_HERMES_HOME_DIR_NAME: Final[str] = "hermes_home"

# Files to seed from ~/.hermes into each agent's HERMES_HOME
_HERMES_HOME_SEED_FILES: Final[tuple[str, ...]] = (
    "config.yaml",
    ".env",
    "auth.json",
    "SOUL.md",
)

# Directories to seed from ~/.hermes into each agent's HERMES_HOME
_HERMES_HOME_SEED_DIRS: Final[tuple[str, ...]] = (
    "memories",
    "skills",
    "home",
)


def _get_user_hermes_dir() -> Path:
    """Return the path to the user's default hermes config directory."""
    return Path.home() / ".hermes"


def _get_local_host(mngr_ctx: MngrContext) -> OnlineHostInterface:
    """Get the local host instance for file operations."""
    local_host_ref = get_provider_instance(LOCAL_PROVIDER_NAME, mngr_ctx).get_host(HostName("localhost"))
    if not isinstance(local_host_ref, OnlineHostInterface):
        raise MngrError("Local host is not online")
    return local_host_ref


def _seed_hermes_home(
    host: OnlineHostInterface,
    source_host: OnlineHostInterface,
    source_hermes_dir: Path,
    target_hermes_home: Path,
) -> None:
    """Transfer seed files and directories from the user's hermes dir to the agent's HERMES_HOME.

    Uses a single rsync call with include/exclude filters to transfer only the
    enumerated config files and directories, skipping runtime state.
    """
    include_args: list[str] = []
    for dir_name in _HERMES_HOME_SEED_DIRS:
        if not (source_hermes_dir / dir_name).exists():
            continue
        include_args.extend([f"--include={dir_name}/", f"--include={dir_name}/**"])
    for file_name in _HERMES_HOME_SEED_FILES:
        if not (source_hermes_dir / file_name).exists():
            continue
        include_args.append(f"--include={file_name}")
    if not include_args:
        return
    include_args.append("--exclude=*")
    with log_span("Seeding hermes home from user config"):
        host.copy_directory(source_host, source_hermes_dir, target_hermes_home, extra_args=" ".join(include_args))


class HermesAgentConfig(AgentTypeConfig):
    """Config for the hermes agent type."""

    command: CommandString = Field(
        default=CommandString("hermes chat"),
        description="Command to run hermes agent",
    )

    def merge_with(self, override: AgentTypeConfig) -> AgentTypeConfig:
        """Merge this config with an override config."""
        if not isinstance(override, HermesAgentConfig):
            raise ConfigParseError("Cannot merge HermesAgentConfig with different agent config type")

        # Merge parent_type (scalar -- override wins if not None)
        merged_parent_type = override.parent_type if override.parent_type is not None else self.parent_type

        # Merge command (scalar -- override wins if not None)
        merged_command = self.command
        if hasattr(override, "command") and override.command is not None:
            merged_command = override.command

        # Merge cli_args (concatenate both tuples)
        merged_cli_args = self.cli_args + override.cli_args if override.cli_args else self.cli_args

        # Merge permissions (list -- concatenate if override is not None)
        merged_permissions = self.permissions
        if override.permissions is not None:
            merged_permissions = list(self.permissions) + list(override.permissions)

        return self.__class__(
            parent_type=merged_parent_type,
            cli_args=merged_cli_args,
            command=merged_command,
            permissions=merged_permissions,
        )


class HermesAgent(BaseAgent[HermesAgentConfig]):
    """Agent implementation for Hermes with isolated HERMES_HOME per agent."""

    def _get_hermes_home_dir(self) -> Path:
        """Return the per-agent HERMES_HOME directory path."""
        return self._get_agent_dir() / _HERMES_HOME_DIR_NAME

    def modify_env_vars(self, host: OnlineHostInterface, env_vars: dict[str, str]) -> None:
        """Inject HERMES_HOME pointing to the per-agent hermes home directory."""
        env_vars["HERMES_HOME"] = str(self._get_hermes_home_dir())

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Seed the per-agent HERMES_HOME from the user's ~/.hermes directory.

        Copies config files, secrets, memories, skills, and home directory
        while skipping runtime state (sessions, logs, plans, etc.).
        Silently skips if ~/.hermes does not exist.
        """
        source_hermes_dir = _get_user_hermes_dir()
        if not source_hermes_dir.exists():
            logger.debug("Skipping hermes home seeding: {} does not exist", source_hermes_dir)
            return

        hermes_home_dir = self._get_hermes_home_dir()
        host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(hermes_home_dir))}", timeout_seconds=5.0)

        # Determine source host: always the local machine running mngr
        if host.is_local:
            source_host = host
        else:
            source_host = _get_local_host(mngr_ctx)

        _seed_hermes_home(host, source_host, source_hermes_dir, hermes_home_dir)


# Module-level hook implementation for pluggy entry point discovery
@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the hermes agent type."""
    return ("hermes", HermesAgent, HermesAgentConfig)
