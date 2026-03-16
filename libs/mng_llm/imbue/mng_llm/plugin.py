"""LLM agent plugin for mng.

Provides the ``llm`` agent type which runs the ``llm`` CLI tool as a managed
agent. Includes provisioning for the llm toolchain, conversation management,
and supporting service infrastructure (chat, web UI, conversation watcher).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import click
from loguru import logger
from pydantic import Field

from imbue.mng import hookimpl
from imbue.mng.agents.base_agent import BaseAgent
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import CommandString
from imbue.mng_llm.provisioning import configure_llm_user_path
from imbue.mng_llm.provisioning import create_mind_conversations_table
from imbue.mng_llm.provisioning import install_llm_toolchain
from imbue.mng_llm.provisioning import provision_llm_tools
from imbue.mng_llm.provisioning import provision_supporting_services
from imbue.mng_llm.settings import load_settings_from_host
from imbue.mng_recursive.provisioning import provision_mng_for_agent

_DEFAULT_LLM_MODEL = "claude-opus-4.6"


def set_uv_tool_env_vars(env_vars: dict[str, str]) -> None:
    """Set UV_TOOL_DIR and UV_TOOL_BIN_DIR from MNG_AGENT_STATE_DIR.

    Ensures that ``uv tool install`` (run by the mng_recursive plugin)
    places binaries into the agent's state directory, and that subsequent
    ``uv tool`` invocations use the same paths. Shared by LlmAgent and
    ClaudeMindAgent.
    """
    agent_state_dir = env_vars.get("MNG_AGENT_STATE_DIR", "")
    if agent_state_dir:
        env_vars["UV_TOOL_DIR"] = f"{agent_state_dir}/tools"
        env_vars["UV_TOOL_BIN_DIR"] = f"{agent_state_dir}/bin"


def set_llm_model_env_var(
    host: OnlineHostInterface,
    work_dir: Path,
    env_vars: dict[str, str],
) -> None:
    """Set MNG_LLM_MODEL from minds.toml settings or hardcoded default.

    Reads the ``[chat] model`` setting from the agent's work directory.
    Falls back to the hardcoded default when the file is missing or
    does not specify a model.

    Shared by LlmAgent and ClaudeMindAgent so that chat.sh can read
    the model from the environment rather than parsing settings itself.
    """
    settings = load_settings_from_host(host, work_dir)
    model = settings.chat.model or _DEFAULT_LLM_MODEL
    env_vars["MNG_LLM_MODEL"] = model


class LlmAgentConfig(AgentTypeConfig):
    """Config for the llm agent type.

    Configures how the llm CLI tool is set up and run as a managed agent.
    """

    command: CommandString = Field(
        default=CommandString("llm"),
        description="The command to run for this agent type.",
    )
    install_llm: bool = Field(
        default=True,
        description="Whether to install llm and its plugins (llm-anthropic, llm-live-chat) during provisioning.",
    )

    def merge_with(self, override: AgentTypeConfig) -> AgentTypeConfig:
        """Merge this config with an override config.

        Scalar fields from the override take precedence when not None.
        """
        if not isinstance(override, LlmAgentConfig):
            return override

        merged_command = self.command
        if hasattr(override, "command") and override.command is not None:
            merged_command = override.command

        return self.__class__(
            command=merged_command,
            install_llm=override.install_llm,
            cli_args=override.cli_args or self.cli_args,
            permissions=override.permissions or self.permissions,
        )


class LlmAgent(BaseAgent[LlmAgentConfig]):
    """Agent class for running the llm CLI tool.

    Extends BaseAgent with llm-specific provisioning:
    - Installs the llm toolchain (llm, llm-anthropic, llm-live-chat)
    - Configures per-agent llm data directory (LLM_USER_PATH)
    - Creates the mind_conversations table for conversation metadata
    - Provisions chat scripts and llm tool functions
    """

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        if command_override is not None:
            raise NotImplementedError("Command overrides are not supported for LlmAgent.")
        if self.agent_config.command != CommandString("llm"):
            raise NotImplementedError("Custom commands are not supported for LlmAgent.")

        parts = ["$MNG_AGENT_STATE_DIR/commands/chat.sh", "--new", "--name", '"Inner Monologue"']
        if self.agent_config.cli_args:
            parts.extend(self.agent_config.cli_args)
        if agent_args:
            parts.extend(agent_args)

        command = CommandString(" ".join(parts))
        logger.trace("Assembled command: {}", command)
        return command

    def modify_env_vars(
        self,
        host: OnlineHostInterface,
        env_vars: dict[str, str],
    ) -> None:
        """Set UV tool dirs and MNG_LLM_MODEL for per-agent tool isolation and chat."""
        set_uv_tool_env_vars(env_vars)
        set_llm_model_env_var(host, self.work_dir, env_vars)

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mng_ctx: MngContext,
    ) -> None:
        """Provision an llm agent with toolchain and conversation infrastructure.

        Steps:
        1. Per-agent mng installation (via mng_recursive)
        2. llm + plugin installation
        3. Per-agent llm data directory
        4. Conversations table in llm database
        5. Supporting service scripts (chat, ttyd dispatch)
        6. LLM tool functions (context gathering for conversations)
        """
        provision_mng_for_agent(agent=self, host=host, mng_ctx=mng_ctx)

        settings = load_settings_from_host(host, self.work_dir)
        provisioning = settings.provisioning
        config = self.agent_config

        if config.install_llm:
            install_llm_toolchain(host, provisioning)

        agent_state_dir = self._get_agent_dir()

        configure_llm_user_path(host, agent_state_dir, provisioning)
        create_mind_conversations_table(host, agent_state_dir, provisioning)
        provision_supporting_services(host, agent_state_dir, provisioning)
        provision_llm_tools(host, agent_state_dir, provisioning)


@hookimpl
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register llm supporting service commands with mng."""
    from imbue.mng_llm.cli import get_all_commands

    return get_all_commands()


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface], type[AgentTypeConfig]]:
    """Register the llm agent type."""
    return ("llm", LlmAgent, LlmAgentConfig)
