from __future__ import annotations

import json
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mng import hookimpl
from imbue.mng.config.agent_class_registry import get_agent_class
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import CommandString
from imbue.mng_claude.claude_config import build_readiness_hooks_config
from imbue.mng_claude.claude_config import merge_hooks_config
from imbue.mng_claude.plugin import ClaudeAgent
from imbue.mng_claude.plugin import ClaudeAgentConfig
from imbue.mng_claude_mind.provisioning import build_memory_sync_hooks_config
from imbue.mng_claude_mind.provisioning import create_mind_symlinks
from imbue.mng_claude_mind.provisioning import provision_claude_settings
from imbue.mng_claude_mind.provisioning import setup_memory_directory
from imbue.mng_claude_mind.settings import load_settings_from_host
from imbue.mng_llm.plugin import set_llm_model_env_var
from imbue.mng_llm.plugin import set_uv_tool_env_vars
from imbue.mng_llm.provisioning import configure_llm_user_path
from imbue.mng_llm.provisioning import create_daily_conversation
from imbue.mng_llm.provisioning import create_mind_conversations_table
from imbue.mng_llm.provisioning import create_slack_notifications_conversation
from imbue.mng_llm.provisioning import create_system_notifications_conversation
from imbue.mng_llm.provisioning import install_llm_toolchain
from imbue.mng_llm.provisioning import provision_llm_tools
from imbue.mng_llm.provisioning import provision_supporting_services
from imbue.mng_llm.provisioning import resolve_work_dir_abs
from imbue.mng_mind.provisioning import provision_default_content
from imbue.mng_recursive.provisioning import provision_mng_for_agent

# Supporting service tmux window names and commands.
# These are run as additional tmux windows alongside the primary role agent.
CONV_WATCHER_WINDOW_NAME: Final[str] = "conv_watcher"
CONV_WATCHER_COMMAND: Final[str] = "mng llmconversations"

EVENT_WATCHER_WINDOW_NAME: Final[str] = "events"
EVENT_WATCHER_COMMAND: Final[str] = "mng mindevents"

# Web server: serves the main web interface with conversation selector
# and agent list page.
WEB_SERVER_WINDOW_NAME: Final[str] = "web_server"
WEB_SERVER_COMMAND: Final[str] = "mng llmweb"


class ClaudeMindConfig(ClaudeAgentConfig):
    """Config for the claude-mind agent type.

    Defaults trust_working_directory to True because minds run
    --in-place in their own repo directory (e.g. ~/.minds/<name>/)
    and should not show the trust dialog on startup.
    """

    trust_working_directory: bool = Field(
        default=True,
        description="Automatically trust the agent's working directory in ~/.claude.json. "
        "Enabled by default for minds since they run in-place in their own repo.",
    )
    install_llm: bool = Field(
        default=True,
        description="Whether to install llm and its plugins (llm-anthropic, llm-live-chat) during provisioning.",
    )


class ClaudeMindAgent(ClaudeAgent):
    """Base agent class for mind role agents built on Claude Code.

    Inherits all Claude Code functionality (session management, provisioning,
    TUI interaction, etc.) and extends it with mind-specific setup:

    At runtime:
    - Overrides assemble_command() to ``cd`` into the active role directory
      before running Claude Code, so .claude/ is discovered naturally

    During provisioning:
    - Installs the llm toolchain (via mng_llm plugin)
    - Provisions default content (via mng_mind plugin)
    - Provisions Claude-specific settings.json and skills symlink
    - Syncs per-role memory/ into Claude project memory via hooks

    Via tmux windows (injected by override_command_options), the following
    supporting services run alongside the role agent:
    - Conversation watcher (syncs llm DB to events/messages/events.jsonl)
    - Event watcher (sends new events to primary role agent via mng message)
    - Web server (main web interface with conversation selector and agent list)
    """

    enter_submission_timeout_seconds: float = Field(
        default=15.0,
        description="Timeout in seconds for waiting on the enter submission signal",
    )

    def _get_mind_config(self) -> ClaudeMindConfig:
        """Get the mind-specific config from this agent.

        Raises RuntimeError if the agent config is not a ClaudeMindConfig,
        which indicates a misconfiguration in the agent type registration.
        """
        if not isinstance(self.agent_config, ClaudeMindConfig):
            raise RuntimeError(
                f"ClaudeMindAgent requires ClaudeMindConfig, got {type(self.agent_config).__name__}. "
                "This indicates the agent type was registered with the wrong config class."
            )
        return self.agent_config

    def _configure_role_hooks(
        self,
        host: OnlineHostInterface,
        active_role: str,
        role_dir_abs: str,
    ) -> None:
        """Write all hooks (readiness + memory sync) to the active role's settings.local.json."""
        settings_path = self.work_dir / active_role / ".claude" / "settings.local.json"

        existing_settings: dict[str, Any] = {}
        try:
            content = host.read_text_file(settings_path)
            existing_settings = json.loads(content)
        except FileNotFoundError:
            pass

        readiness_config = build_readiness_hooks_config()
        merged = merge_hooks_config(existing_settings, readiness_config)
        if merged is not None:
            existing_settings = merged

        memory_config = build_memory_sync_hooks_config(role_dir_abs)
        merged = merge_hooks_config(existing_settings, memory_config)
        if merged is not None:
            existing_settings = merged

        with log_span("Configuring hooks in {}", settings_path):
            host.write_text_file(settings_path, json.dumps(existing_settings, indent=2) + "\n")

    @staticmethod
    def _get_role_from_env(options: CreateAgentOptions) -> str:
        """Read the ROLE env var from the agent's environment options."""
        for env_var in options.environment.env_vars:
            if env_var.key == "ROLE":
                return env_var.value
        raise RuntimeError(
            "ROLE environment variable is required for mind agents. "
            "Pass --env ROLE=<role> (e.g. --env ROLE=thinking) when creating the agent."
        )

    def modify_env_vars(
        self,
        host: OnlineHostInterface,
        env_vars: dict[str, str],
    ) -> None:
        """Set UV tool dirs and MNG_LLM_MODEL for per-agent tool isolation and chat."""
        super().modify_env_vars(host, env_vars)
        set_uv_tool_env_vars(env_vars)
        set_llm_model_env_var(host, self.work_dir, env_vars)

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Prepend ``cd "$ROLE" &&`` so Claude Code runs from within the role directory."""
        base_command = super().assemble_command(host, agent_args, command_override)
        return CommandString(f'cd "$ROLE" && {base_command}')

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mng_ctx: MngContext,
    ) -> None:
        """Provision a mind role agent with llm toolchain and supporting service infrastructure.

        Extends ClaudeAgent provisioning with:
        1. Per-agent mng installation (via mng_recursive, before super())
        2. Settings loading from minds.toml
        3. llm + plugin installation (via mng_llm)
        4. Default content (GLOBAL.md, role prompts, skills) via mng_mind
        5. Claude-specific settings.json injection
        6. Symlinks (CLAUDE.md -> GLOBAL.md, CLAUDE.local.md -> PROMPT.md, .claude/skills -> skills)
        7. All hooks (readiness + memory sync) written to <role>/.claude/settings.local.json
        8. Supporting service scripts and chat utilities (via mng_llm)
        9. LLM tool scripts for conversation context (via mng_llm)
        10. Per-role memory directory setup
        """

        provision_mng_for_agent(agent=self, host=host, mng_ctx=mng_ctx)

        super().provision(host, options, mng_ctx)

        config = self._get_mind_config()
        active_role = self._get_role_from_env(options)

        settings = load_settings_from_host(host, self.work_dir)
        provisioning = settings.provisioning

        if config.install_llm:
            install_llm_toolchain(host, provisioning)

        provision_default_content(host, self.work_dir, provisioning)
        provision_claude_settings(host, self.work_dir, active_role, provisioning)
        create_mind_symlinks(host, self.work_dir, active_role, provisioning)

        work_dir_abs = resolve_work_dir_abs(host, self.work_dir, provisioning)
        role_dir_abs = f"{work_dir_abs}/{active_role}"
        self._configure_role_hooks(host, active_role, role_dir_abs)

        agent_state_dir = self._get_agent_dir()

        provision_supporting_services(host, agent_state_dir, provisioning)
        provision_llm_tools(host, agent_state_dir, provisioning)

        configure_llm_user_path(host, agent_state_dir, provisioning)
        create_mind_conversations_table(host, agent_state_dir, provisioning)

        if config.install_llm:
            create_system_notifications_conversation(host, agent_state_dir, provisioning)
            create_slack_notifications_conversation(host, agent_state_dir, provisioning)
            chat_model = settings.chat.model or "claude-opus-4.6"
            create_daily_conversation(host, agent_state_dir, provisioning, chat_model)

        setup_memory_directory(host, self.work_dir, active_role, role_dir_abs, provisioning)


def inject_supporting_services(params: dict[str, Any]) -> None:
    """Inject all mind supporting service tmux windows into the create command parameters."""
    existing = params.get("extra_window", ())
    params["extra_window"] = (
        *existing,
        f'{CONV_WATCHER_WINDOW_NAME}="{CONV_WATCHER_COMMAND}"',
        f'{EVENT_WATCHER_WINDOW_NAME}="{EVENT_WATCHER_COMMAND}"',
        f'{WEB_SERVER_WINDOW_NAME}="{WEB_SERVER_COMMAND}"',
    )


def get_agent_type_from_params(params: dict[str, Any]) -> str | None:
    """Extract the agent type from create command parameters."""
    return params.get("type") or params.get("positional_agent_type")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface], type[AgentTypeConfig]]:
    """Register the claude-mind agent type."""
    return ("claude-mind", ClaudeMindAgent, ClaudeMindConfig)


def _is_claude_mind_agent_type(agent_type_name: str) -> bool:
    """Check whether the given agent type name resolves to a ClaudeMindAgent subclass."""
    try:
        agent_class = get_agent_class(agent_type_name)
    except MngError as e:
        logger.debug("Could not resolve agent type '{}': {}", agent_type_name, e)
        return False
    return issubclass(agent_class, ClaudeMindAgent)


@hookimpl
def override_command_options(
    command_name: str,
    command_class: type,
    params: dict[str, Any],
) -> None:
    """Add mind supporting service windows when creating claude-mind role agents (or subtypes)."""
    if command_name != "create":
        return

    agent_type = get_agent_type_from_params(params)
    if agent_type is None:
        return

    if not _is_claude_mind_agent_type(agent_type):
        return

    inject_supporting_services(params)
