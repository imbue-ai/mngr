from __future__ import annotations

from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.mng import hookimpl
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgent
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgentConfig
from imbue.mng.config.agent_class_registry import get_agent_class
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_claude_zygote.provisioning import configure_llm_user_path
from imbue.mng_claude_zygote.provisioning import create_changeling_symlinks
from imbue.mng_claude_zygote.provisioning import create_daily_conversation
from imbue.mng_claude_zygote.provisioning import create_event_log_directories
from imbue.mng_claude_zygote.provisioning import create_system_notifications_conversation
from imbue.mng_claude_zygote.provisioning import install_llm_toolchain
from imbue.mng_claude_zygote.provisioning import link_memory_directory
from imbue.mng_claude_zygote.provisioning import provision_changeling_scripts
from imbue.mng_claude_zygote.provisioning import provision_default_content
from imbue.mng_claude_zygote.provisioning import provision_llm_tools
from imbue.mng_claude_zygote.provisioning import validate_talking_role_constraints
from imbue.mng_claude_zygote.provisioning import warn_if_mng_unavailable
from imbue.mng_claude_zygote.settings import load_settings_from_host
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
# 4. Writes a servers/events.jsonl record so the changelings forwarding server can discover it
_AGENT_TTYD_INVOCATION = (
    "_SESSION=$(tmux display-message -p '#{session_name}') && "
    'ttyd -p 0 -t disableLeaveAlert=true -W bash -c \'unset TMUX && exec tmux attach -t "$1":0\' -- "$_SESSION"'
)

AGENT_TTYD_COMMAND: Final[str] = build_ttyd_server_command(_AGENT_TTYD_INVOCATION, AGENT_TTYD_SERVER_NAME)

# Watcher tmux window names and commands.
# These are run as additional tmux windows alongside the primary agent.
CONV_WATCHER_WINDOW_NAME: Final[str] = "conv_watcher"
CONV_WATCHER_COMMAND: Final[str] = "python3 $MNG_HOST_DIR/commands/conversation_watcher.py"

EVENT_WATCHER_WINDOW_NAME: Final[str] = "events"
EVENT_WATCHER_COMMAND: Final[str] = "python3 $MNG_HOST_DIR/commands/event_watcher.py"

TRANSCRIPT_WATCHER_WINDOW_NAME: Final[str] = "transcript"
TRANSCRIPT_WATCHER_COMMAND: Final[str] = "python3 $MNG_HOST_DIR/commands/transcript_watcher.py"

# Web server: serves the main web interface with conversation selector
# and agent list page.
WEB_SERVER_WINDOW_NAME: Final[str] = "web_server"
WEB_SERVER_COMMAND: Final[str] = 'python3 "$MNG_HOST_DIR/commands/web_server.py"'

# Chat ttyd: a ttyd with --url-arg that dispatches to chat.sh.
# Accessed with ?arg=<conversation_id> to resume, or no arg for a new conversation.
CHAT_TTYD_WINDOW_NAME: Final[str] = "chat"
CHAT_TTYD_SERVER_NAME: Final[str] = CHAT_TTYD_WINDOW_NAME
_CHAT_TTYD_INVOCATION: Final[str] = (
    'ttyd -p 0 -a -t disableLeaveAlert=true -W bash "$MNG_HOST_DIR/commands/chat_ttyd_handler.sh"'
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
    - Creates symlinks for Claude Code discovery (CLAUDE.md, settings, skills)
    - Provisions watcher scripts and chat utilities
    - Sets up event log directories (events/<source>/events.jsonl)
    - Symlinks memory/ into Claude project memory

    Via tmux windows (injected by override_command_options):
    - Conversation watcher (syncs llm DB to events/messages/events.jsonl)
    - Event watcher (sends new events to primary agent via mng message)
    - Web server (main web interface with conversation selector and agent list)
    - Chat ttyd (--url-arg ttyd for conversation terminal access)
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
        2. Talking role constraint validation (no skills or settings allowed)
        3. llm + plugin installation
        4. Default content (GLOBAL.md, talking/PROMPT.md, thinking/PROMPT.md,
           thinking/settings.json, skills)
        5. Symlinks for Claude Code discovery (CLAUDE.md, settings, skills)
        6. Watcher scripts and chat utilities
        7. Event log directory structure (events/<source>/events.jsonl)
        8. LLM tool scripts for conversation context
        9. Memory directory symlink into Claude project
        """
        super().provision(host, options, mng_ctx)

        config = self._get_zygote_config()

        # Load settings from .changelings/settings.toml (falls back to defaults)
        settings = load_settings_from_host(host, self.work_dir, config.changelings_dir_name)
        provisioning = settings.provisioning

        warn_if_mng_unavailable(host, mng_ctx.pm, provisioning)
        validate_talking_role_constraints(host, self.work_dir, provisioning)

        if config.install_llm:
            install_llm_toolchain(host, provisioning)

        provision_default_content(host, self.work_dir, provisioning)
        create_changeling_symlinks(host, self.work_dir, provisioning)

        # Re-configure readiness hooks AFTER symlinks are created.
        # create_changeling_symlinks replaces .claude/settings.local.json
        # with a symlink to thinking/settings.json (via ln -sf), which
        # destroys the hooks that ClaudeAgent.provision() wrote there.
        # Re-running _configure_readiness_hooks writes the hooks through
        # the symlink into thinking/settings.json.
        self._configure_readiness_hooks(host)

        provision_changeling_scripts(host, provisioning)
        provision_llm_tools(host, provisioning)

        agent_state_dir = self._get_agent_dir()
        create_event_log_directories(host, agent_state_dir, provisioning)

        configure_llm_user_path(host, agent_state_dir, provisioning)

        if config.install_llm:
            create_system_notifications_conversation(host, agent_state_dir, provisioning)
            chat_model = settings.chat.model or "claude-opus-4-6"
            create_daily_conversation(host, agent_state_dir, provisioning, chat_model)

        link_memory_directory(host, self.work_dir, provisioning)


def inject_changeling_windows(params: dict[str, Any]) -> None:
    """Inject all changeling tmux windows into the create command parameters.

    Adds:
    - Agent ttyd (web terminal for the primary agent's tmux session)
    - Conversation watcher (syncs llm DB to JSONL files)
    - Event watcher (sends new events to primary agent via mng message)
    - Web server (main web interface with conversation selector and agent list)
    - Chat ttyd (--url-arg ttyd for conversation access)
    - Transcript watcher (converts claude_transcript to common_transcript)
    """
    existing = params.get("add_command", ())
    params["add_command"] = (
        *existing,
        f'{AGENT_TTYD_WINDOW_NAME}="{AGENT_TTYD_COMMAND}"',
        f'{CONV_WATCHER_WINDOW_NAME}="{CONV_WATCHER_COMMAND}"',
        f'{EVENT_WATCHER_WINDOW_NAME}="{EVENT_WATCHER_COMMAND}"',
        f'{WEB_SERVER_WINDOW_NAME}="{WEB_SERVER_COMMAND}"',
        f'{TRANSCRIPT_WATCHER_WINDOW_NAME}="{TRANSCRIPT_WATCHER_COMMAND}"',
        f'{CHAT_TTYD_WINDOW_NAME}="{CHAT_TTYD_COMMAND}"',
    )


def get_agent_type_from_params(params: dict[str, Any]) -> str | None:
    """Extract the agent type from create command parameters."""
    return params.get("agent_type") or params.get("positional_agent_type")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface], type[AgentTypeConfig]]:
    """Register the claude-zygote agent type."""
    return ("claude-zygote", ClaudeZygoteAgent, ClaudeZygoteConfig)


def _is_claude_zygote_agent_type(agent_type_name: str) -> bool:
    """Check whether the given agent type name resolves to a ClaudeZygoteAgent subclass."""
    try:
        agent_class = get_agent_class(agent_type_name)
    except MngError as e:
        logger.debug("Could not resolve agent type '{}': {}", agent_type_name, e)
        return False
    return issubclass(agent_class, ClaudeZygoteAgent)


@hookimpl
def override_command_options(
    command_name: str,
    command_class: type,
    params: dict[str, Any],
) -> None:
    """Add changeling tmux windows when creating claude-zygote agents (or subtypes).

    Injects: agent ttyd, conversation watcher, event watcher, web server,
    and chat ttyd.

    Matches any agent type whose registered class is ClaudeZygoteAgent or
    a subclass of it (e.g. elena-code, custom changeling types).
    """
    if command_name != "create":
        return

    agent_type = get_agent_type_from_params(params)
    if agent_type is None:
        return

    if not _is_claude_zygote_agent_type(agent_type):
        return

    inject_changeling_windows(params)
