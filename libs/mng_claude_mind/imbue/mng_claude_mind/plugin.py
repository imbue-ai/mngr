from __future__ import annotations

import json
from typing import Any
from typing import ClassVar
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
from imbue.mng_claude_mind.data_types import ClaudeMindSettings
from imbue.mng_claude_mind.provisioning import build_stop_hook_config
from imbue.mng_claude_mind.provisioning import create_mind_symlinks
from imbue.mng_claude_mind.provisioning import provision_claude_settings
from imbue.mng_claude_mind.provisioning import provision_event_exclude_sources
from imbue.mng_claude_mind.provisioning import provision_stop_hook_script
from imbue.mng_claude_mind.provisioning import run_link_skills_script
from imbue.mng_claude_mind.provisioning import setup_memory_directory
from imbue.mng_llm.data_types import ProvisioningSettings
from imbue.mng_llm.plugin import set_llm_model_env_var
from imbue.mng_llm.plugin import set_uv_tool_env_vars
from imbue.mng_llm.provisioning import check_llm_toolchain
from imbue.mng_llm.provisioning import configure_llm_user_path
from imbue.mng_llm.provisioning import create_first_daily_conversation
from imbue.mng_llm.provisioning import create_mind_conversations_table
from imbue.mng_llm.provisioning import create_slack_notifications_conversation
from imbue.mng_llm.provisioning import create_system_notifications_conversation
from imbue.mng_llm.provisioning import create_work_log_conversation
from imbue.mng_llm.provisioning import install_llm_toolchain
from imbue.mng_llm.provisioning import provision_llm_tools
from imbue.mng_llm.provisioning import provision_supporting_services
from imbue.mng_llm.provisioning import resolve_work_dir_abs
from imbue.mng_llm.settings import load_from_host
from imbue.mng_mind.data_types import SOURCE_COMMON_TRANSCRIPT
from imbue.mng_mind.provisioning import provision_link_skills_script_file
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

# Observer: runs 'mng observe' writing events to the agent's state directory
# so each mind has its own local copy of agent state events.
OBSERVER_WINDOW_NAME: Final[str] = "observer"
OBSERVER_COMMAND: Final[str] = 'mng observe --events-dir "$MNG_AGENT_STATE_DIR"'


class ClaudeMindConfig(ClaudeAgentConfig):
    """Config for the claude-mind agent type.

    Defaults trust_working_directory to True because minds run
    --transfer=none in their own repo directory (e.g. ~/.minds/<name>/)
    and should not show the trust dialog on startup.
    """

    trust_working_directory: bool = Field(
        default=True,
        description="Automatically trust the agent's working directory in ~/.claude.json. "
        "Enabled by default for minds since they run in-place in their own repo.",
    )
    sync_home_settings: bool = Field(
        default=False,
        description="Whether to sync Claude settings from ~/.claude/ to the per-agent config dir",
    )
    sync_claude_json: bool = Field(
        default=False,
        description="Whether to sync the local ~/.claude.json to a remote host (useful for API key settings and permissions)",
    )
    sync_claude_credentials: bool = Field(
        default=False,
        description="Whether to sync the local ~/.claude/.credentials.json to the per-agent config dir",
    )
    symlink_user_resources: bool = Field(
        default=False,
        description="Whether to symlink (True) or copy (False) user resources from ~/.claude/ "
        "into local per-agent config dirs. Symlinks avoid duplication and keep the "
        "per-agent dir lightweight; copies provide full isolation.",
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
    - Provisions the link_skills.sh script (via mng_mind plugin)
    - Provisions Claude-specific settings.json and skills symlink
    - Sets autoMemoryDirectory to point to per-role memory/

    Via tmux windows (injected by override_command_options), the following
    supporting services run alongside the role agent:
    - Conversation watcher (syncs llm DB to events/messages/events.jsonl)
    - Event watcher (sends new events to primary role agent via mng message)
    - Web server (main web interface with conversation selector and agent list)
    - Observer (runs mng observe writing agent state events to the agent state directory)
    """

    _settings_class: ClassVar[type[ClaudeMindSettings]] = ClaudeMindSettings

    agent_config: ClaudeMindConfig = Field(frozen=True, repr=False, description="Agent type config")

    def load_settings_from_host(self, host: OnlineHostInterface) -> ClaudeMindSettings:
        """Load ClaudeMindSettings from minds.toml on the host.

        Returns settings with defaults for any missing values.
        Uses ClaudeMindSettings (not LlmSettings) so that mind-specific fields
        like agent_type, watchers, and vendor are parsed correctly.
        """
        return load_from_host(host, self.work_dir, self._settings_class)

    def _configure_role_settings(
        self,
        host: OnlineHostInterface,
        active_role: str,
        role_dir_abs: str,
        settings: ProvisioningSettings,
    ) -> None:
        """Write hooks and required settings to the active role's settings.local.json."""
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

        stop_hook_path = provision_stop_hook_script(host, self.work_dir, active_role, settings)
        stop_config = build_stop_hook_config(stop_hook_path)
        merged = merge_hooks_config(existing_settings, stop_config)
        if merged is not None:
            existing_settings = merged

        existing_settings["autoMemoryDirectory"] = f"{role_dir_abs}/memory"

        with log_span("Configuring settings in {}", settings_path):
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
        settings = self.load_settings_from_host(host)
        set_llm_model_env_var(settings, env_vars)

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Prepend ``cd "$ROLE" &&`` so Claude Code runs from within the role directory."""
        base_command = super().assemble_command(host, agent_args, command_override)
        return CommandString(f'cd "$ROLE" && ( {base_command} )')

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
        4. link_skills.sh script (via mng_mind)
        5. Symlink shared skills into active role's skills directory
        6. Claude-specific settings.json injection
        7. Symlinks (CLAUDE.md -> GLOBAL.md, CLAUDE.local.md -> PROMPT.md, .claude/skills -> skills)
        8. All hooks (readiness + stop) and settings written to <role>/.claude/settings.local.json
        9. Supporting service scripts and chat utilities (via mng_llm)
        10. LLM tool scripts for conversation context (via mng_llm)
        11. Per-role memory directory setup
        """

        provision_mng_for_agent(agent=self, host=host, mng_ctx=mng_ctx)

        super().provision(host, options, mng_ctx)

        config = self.agent_config
        active_role = self._get_role_from_env(options)

        provision_event_exclude_sources(host, self.work_dir, exclude_sources=(SOURCE_COMMON_TRANSCRIPT,))

        settings = self.load_settings_from_host(host)
        provisioning = settings.provisioning

        if config.install_llm:
            install_llm_toolchain(host, provisioning)
        else:
            check_llm_toolchain(host, provisioning)

        provision_link_skills_script_file(host, self.work_dir, provisioning)
        run_link_skills_script(host, self.work_dir, active_role, provisioning)
        provision_claude_settings(host, self.work_dir, active_role, provisioning)
        create_mind_symlinks(host, self.work_dir, active_role, provisioning)

        work_dir_abs = resolve_work_dir_abs(host, self.work_dir, provisioning)
        role_dir_abs = f"{work_dir_abs}/{active_role}"
        self._configure_role_settings(host, active_role, role_dir_abs, provisioning)

        agent_state_dir = self._get_agent_dir()

        provision_supporting_services(host, agent_state_dir, provisioning)
        provision_llm_tools(host, agent_state_dir, provisioning)

        configure_llm_user_path(host, agent_state_dir, provisioning)
        create_mind_conversations_table(host, agent_state_dir, provisioning)

        create_system_notifications_conversation(host, agent_state_dir, provisioning)
        create_slack_notifications_conversation(host, agent_state_dir, provisioning)
        chat_model = settings.chat.model or "claude-haiku-4.5"
        create_work_log_conversation(host, agent_state_dir, provisioning, chat_model)
        create_first_daily_conversation(host, agent_state_dir, provisioning, chat_model, settings.chat.welcome_message)

        setup_memory_directory(host, self.work_dir, active_role, provisioning)

    def _build_per_agent_claude_json(self, options: CreateAgentOptions, config: ClaudeAgentConfig) -> dict[str, Any]:
        data = super()._build_per_agent_claude_json(options, config)
        # FOLLOWUP: we can remove this eventually (once the agents are started inside VMs, it will be set properly anyway)
        data["bypassPermissionsModeAccepted"] = True
        return data


def inject_supporting_services(params: dict[str, Any]) -> None:
    """Inject all mind supporting service tmux windows into the create command parameters."""
    existing = params.get("extra_window", ())
    params["extra_window"] = (
        *existing,
        f'{CONV_WATCHER_WINDOW_NAME}="{CONV_WATCHER_COMMAND}"',
        f'{EVENT_WATCHER_WINDOW_NAME}="{EVENT_WATCHER_COMMAND}"',
        f'{WEB_SERVER_WINDOW_NAME}="{WEB_SERVER_COMMAND}"',
        f'{OBSERVER_WINDOW_NAME}="{OBSERVER_COMMAND}"',
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
