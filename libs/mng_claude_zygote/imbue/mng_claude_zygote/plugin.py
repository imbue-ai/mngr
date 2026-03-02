from __future__ import annotations

from typing import Any
from typing import Final

from pydantic import Field

from imbue.mng import hookimpl
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgent
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgentConfig
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_claude_zygote.data_types import ChatModel
from imbue.mng_claude_zygote.provisioning import create_changeling_symlinks
from imbue.mng_claude_zygote.provisioning import create_event_log_directories
from imbue.mng_claude_zygote.provisioning import install_llm_toolchain
from imbue.mng_claude_zygote.provisioning import link_memory_directory
from imbue.mng_claude_zygote.provisioning import provision_changeling_scripts
from imbue.mng_claude_zygote.provisioning import provision_default_content
from imbue.mng_claude_zygote.provisioning import provision_llm_tools
from imbue.mng_claude_zygote.provisioning import warn_if_mng_unavailable
from imbue.mng_claude_zygote.settings import load_settings_from_host
from imbue.mng_claude_zygote.settings import provision_settings_file
from imbue.mng_ttyd.plugin import build_ttyd_server_command

AGENT_TTYD_WINDOW_NAME: Final[str] = "agent"
AGENT_TTYD_SERVER_NAME: Final[str] = AGENT_TTYD_WINDOW_NAME

# Bash wrapper that starts ttyd attached to the agent's own tmux session.
# This allows users to interact with the Claude agent via a web browser.
#
# How it works:
# 1. Gets the current tmux session name (the agent's session)
# 2. Starts ttyd on a random port (-p 0) running `tmux attach` to that session
#    - Unsets TMUX env var so tmux allows the nested attach from ttyd's child process
# 3. Watches ttyd's stderr for the assigned port number (via shared helper)
# 4. Writes a servers.jsonl record so the changelings forwarding server can discover it
_AGENT_TTYD_INVOCATION = (
    "_SESSION=$(tmux display-message -p '#{session_name}') && "
    'ttyd -p 0 -t disableLeaveAlert=true -W bash -c \'unset TMUX && exec tmux attach -t "$1":0\' -- "$_SESSION"'
)

AGENT_TTYD_COMMAND: Final[str] = build_ttyd_server_command(_AGENT_TTYD_INVOCATION, AGENT_TTYD_SERVER_NAME)

# Watcher tmux window names and commands.
# These are run as additional tmux windows alongside the primary agent.
CONV_WATCHER_WINDOW_NAME: Final[str] = "conv_watcher"
CONV_WATCHER_COMMAND: Final[str] = "$MNG_HOST_DIR/commands/conversation_watcher.sh"

EVENT_WATCHER_WINDOW_NAME: Final[str] = "events"
EVENT_WATCHER_COMMAND: Final[str] = "$MNG_HOST_DIR/commands/event_watcher.sh"

# Conversation ttyd: a web terminal that runs the chat script for interactive
# conversation access via the browser.
CHAT_TTYD_WINDOW_NAME: Final[str] = "chat"
CHAT_TTYD_SERVER_NAME: Final[str] = CHAT_TTYD_WINDOW_NAME
_CHAT_TTYD_INVOCATION: Final[str] = (
    "ttyd -p 0 -t disableLeaveAlert=true -W bash -c 'exec \"$MNG_HOST_DIR/commands/chat.sh\"'"
)
CHAT_TTYD_COMMAND: Final[str] = build_ttyd_server_command(_CHAT_TTYD_INVOCATION, CHAT_TTYD_SERVER_NAME)


class ClaudeZygoteConfig(ClaudeAgentConfig):
    """Config for the claude-zygote agent type.

    Defaults trust_working_directory to True because changelings run
    --in-place in their own repo directory (e.g. ~/.changelings/<name>/)
    and should not show the trust dialog on startup.
    """

    trust_working_directory: bool = Field(
        default=True,
        description="Automatically trust the agent's working directory in ~/.claude.json. "
        "Enabled by default for changelings since they run in-place in their own repo.",
    )
    default_chat_model: ChatModel = Field(
        default=ChatModel("claude-opus-4-6"),
        description="Default model for new conversation threads.",
    )
    install_llm: bool = Field(
        default=True,
        description="Whether to install llm and its plugins (llm-anthropic, llm-live-chat) during provisioning.",
    )
    changelings_dir_name: str = Field(
        default=".changelings",
        description="Name of the changelings configuration directory in the agent repo.",
    )


class ClaudeZygoteAgent(ClaudeAgent):
    """Base agent for changeling agents built on Claude Code.

    Inherits all Claude Code functionality (session management, provisioning,
    TUI interaction, etc.) and extends it with changeling-specific setup:

    During provisioning:
    - Installs the llm toolchain (llm, llm-anthropic, llm-live-chat)
    - Creates symlinks for changeling entrypoint files
    - Provisions watcher scripts and chat utilities
    - Sets up event log directories (logs/<source>/events.jsonl)
    - Symlinks .changelings/memory/ into Claude project memory

    Via tmux windows (injected by override_command_options):
    - Conversation watcher (syncs llm DB to logs/messages/events.jsonl)
    - Event watcher (sends new events to primary agent via mng message)
    - Chat ttyd (web terminal for conversation access)
    """

    def _get_zygote_config(self) -> ClaudeZygoteConfig:
        """Get the zygote-specific config from this agent.

        Raises RuntimeError if the agent config is not a ClaudeZygoteConfig,
        which indicates a misconfiguration in the agent type registration.
        """
        if not isinstance(self.agent_config, ClaudeZygoteConfig):
            raise RuntimeError(
                f"ClaudeZygoteAgent requires ClaudeZygoteConfig, got {type(self.agent_config).__name__}. "
                "This indicates the agent type was registered with the wrong config class."
            )
        return self.agent_config

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mng_ctx: MngContext,
    ) -> None:
        """Provision the changeling agent with llm toolchain and watcher infrastructure.

        Extends ClaudeAgent provisioning with:
        1. Settings loading from .changelings/settings.toml
        2. llm + plugin installation
        3. Default content (CLAUDE.md, entrypoint files, skills) if missing
        4. Symlinks for .changelings/entrypoint.md -> CLAUDE.local.md
        5. Watcher scripts and chat utilities
        6. Event log directory structure (logs/<source>/events.jsonl)
        7. Default chat model configuration
        8. LLM tool scripts for conversation context
        9. Memory directory symlink into Claude project
        10. Settings file provisioned to agent state dir for script access
        """
        super().provision(host, options, mng_ctx)

        config = self._get_zygote_config()

        # Load settings from .changelings/settings.toml (falls back to defaults)
        settings = load_settings_from_host(host, self.work_dir, config.changelings_dir_name)
        provisioning = settings.provisioning

        warn_if_mng_unavailable(host, mng_ctx.pm, provisioning)

        if config.install_llm:
            install_llm_toolchain(host, provisioning)

        provision_default_content(host, self.work_dir, config.changelings_dir_name, provisioning)
        create_changeling_symlinks(host, self.work_dir, config.changelings_dir_name, provisioning)
        provision_changeling_scripts(host, provisioning)
        provision_llm_tools(host, provisioning)

        agent_state_dir = self._get_agent_dir()
        create_event_log_directories(host, agent_state_dir, provisioning)

        link_memory_directory(host, self.work_dir, config.changelings_dir_name, provisioning)

        # Provision settings.toml to agent state dir so scripts can access it
        provision_settings_file(host, self.work_dir, config.changelings_dir_name, agent_state_dir)


def inject_agent_ttyd(params: dict[str, Any]) -> None:
    """Inject an agent ttyd window into the create command parameters.

    This adds a ttyd web terminal that attaches to the agent's tmux session,
    allowing users to interact with the Claude agent via a web browser.

    Intended to be called from override_command_options hooks by plugins
    that register ClaudeZygoteAgent subtypes.
    """
    existing = params.get("add_command", ())
    params["add_command"] = (*existing, f'{AGENT_TTYD_WINDOW_NAME}="{AGENT_TTYD_COMMAND}"')


def inject_changeling_windows(params: dict[str, Any]) -> None:
    """Inject all changeling tmux windows into the create command parameters.

    Adds:
    - Agent ttyd (web terminal for the primary agent)
    - Conversation watcher (syncs llm DB to JSONL files)
    - Event watcher (sends new events to primary agent via mng message)
    - Chat ttyd (web terminal for conversation access)
    """
    inject_agent_ttyd(params)

    existing = params.get("add_command", ())
    params["add_command"] = (
        *existing,
        f'{CONV_WATCHER_WINDOW_NAME}="{CONV_WATCHER_COMMAND}"',
        f'{EVENT_WATCHER_WINDOW_NAME}="{EVENT_WATCHER_COMMAND}"',
        f'{CHAT_TTYD_WINDOW_NAME}="{CHAT_TTYD_COMMAND}"',
    )


def get_agent_type_from_params(params: dict[str, Any]) -> str | None:
    """Extract the agent type from create command parameters."""
    return params.get("agent_type") or params.get("positional_agent_type")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface], type[AgentTypeConfig]]:
    """Register the claude-zygote agent type."""
    return ("claude-zygote", ClaudeZygoteAgent, ClaudeZygoteConfig)


@hookimpl
def override_command_options(
    command_name: str,
    command_class: type,
    params: dict[str, Any],
) -> None:
    """Add changeling tmux windows when creating claude-zygote agents.

    Injects: agent ttyd, conversation watcher, event watcher, and chat ttyd.
    """
    if command_name != "create":
        return

    agent_type = get_agent_type_from_params(params)
    if agent_type != "claude-zygote":
        return

    inject_changeling_windows(params)
