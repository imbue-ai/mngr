from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from typing import Final

import click
from loguru import logger
from pydantic import Field

from imbue.imbue_common.logging import log_span
from imbue.mng import hookimpl
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgent
from imbue.mng.agents.default_plugins.claude_agent import ClaudeAgentConfig
from imbue.mng.agents.default_plugins.claude_config import build_readiness_hooks_config
from imbue.mng.agents.default_plugins.claude_config import merge_hooks_config
from imbue.mng.config.agent_class_registry import get_agent_class
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import CommandString
from imbue.mng_claude_changeling.provisioning import build_memory_sync_hooks_config
from imbue.mng_claude_changeling.provisioning import configure_llm_user_path
from imbue.mng_claude_changeling.provisioning import create_changeling_conversations_table
from imbue.mng_claude_changeling.provisioning import create_changeling_symlinks
from imbue.mng_claude_changeling.provisioning import create_daily_conversation
from imbue.mng_claude_changeling.provisioning import create_event_log_directories
from imbue.mng_claude_changeling.provisioning import create_slack_notifications_conversation
from imbue.mng_claude_changeling.provisioning import create_system_notifications_conversation
from imbue.mng_claude_changeling.provisioning import install_llm_toolchain
from imbue.mng_claude_changeling.provisioning import provision_default_content
from imbue.mng_claude_changeling.provisioning import provision_llm_tools
from imbue.mng_claude_changeling.provisioning import provision_supporting_services
from imbue.mng_claude_changeling.provisioning import resolve_work_dir_abs
from imbue.mng_claude_changeling.provisioning import setup_memory_directory
from imbue.mng_claude_changeling.provisioning import validate_talking_role_constraints
from imbue.mng_claude_changeling.settings import load_settings_from_host
from imbue.mng_ttyd.plugin import build_ttyd_server_command

AGENT_TTYD_WINDOW_NAME: Final[str] = "agent"
AGENT_TTYD_SERVER_NAME: Final[str] = AGENT_TTYD_WINDOW_NAME

# Bash wrapper that starts ttyd attached to the role agent's own tmux session.
# This allows users to interact with the role agent via a web browser.
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

# Supporting service tmux window names and commands.
# These are run as additional tmux windows alongside the primary role agent.
CONV_WATCHER_WINDOW_NAME: Final[str] = "conv_watcher"
CONV_WATCHER_COMMAND: Final[str] = "mng changelingconversations"

EVENT_WATCHER_WINDOW_NAME: Final[str] = "events"
EVENT_WATCHER_COMMAND: Final[str] = "mng changelingevents"

TRANSCRIPT_WATCHER_WINDOW_NAME: Final[str] = "transcript"
TRANSCRIPT_WATCHER_COMMAND: Final[str] = "mng changelingtranscript"

# Web server: serves the main web interface with conversation selector
# and agent list page.
WEB_SERVER_WINDOW_NAME: Final[str] = "web_server"
WEB_SERVER_COMMAND: Final[str] = "mng changelingweb"

# Chat ttyd: a ttyd with --url-arg that dispatches to chat.sh.
# Accessed with ?arg=<conversation_id> to resume, or no arg for a new conversation.
CHAT_TTYD_WINDOW_NAME: Final[str] = "chat"
CHAT_TTYD_SERVER_NAME: Final[str] = CHAT_TTYD_WINDOW_NAME
_CHAT_TTYD_INVOCATION: Final[str] = (
    'ttyd -p 0 -a -t disableLeaveAlert=true -W bash "$MNG_AGENT_STATE_DIR/commands/chat_ttyd_handler.sh"'
)
CHAT_TTYD_COMMAND: Final[str] = build_ttyd_server_command(_CHAT_TTYD_INVOCATION, CHAT_TTYD_SERVER_NAME)


class ClaudeChangelingConfig(ClaudeAgentConfig):
    """Config for the claude-changeling agent type.

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


class ClaudeChangelingAgent(ClaudeAgent):
    """Base agent class for changeling role agents built on Claude Code.

    Inherits all Claude Code functionality (session management, provisioning,
    TUI interaction, etc.) and extends it with changeling-specific setup:

    At runtime:
    - Overrides assemble_command() to ``cd`` into the active role directory
      before running Claude Code, so .claude/ is discovered naturally

    During provisioning:
    - Installs the llm toolchain (llm, llm-anthropic, llm-live-chat)
    - Provisions supporting service scripts and chat utilities
    - Sets up event log directories (events/<source>/events.jsonl)
    - Syncs per-role memory/ into Claude project memory via hooks

    Via tmux windows (injected by override_command_options), the following
    supporting services run alongside the role agent:
    - Conversation watcher (syncs llm DB to events/messages/events.jsonl)
    - Event watcher (sends new events to primary role agent via mng message)
    - Web server (main web interface with conversation selector and agent list)
    - Chat ttyd (--url-arg ttyd for conversation terminal access)
    """

    enter_submission_timeout_seconds: float = Field(
        # increased timeout because we don't want to send duplicate events if we can avoid it
        default=15.0,
        description="Timeout in seconds for waiting on the enter submission signal",
    )

    def _get_changeling_config(self) -> ClaudeChangelingConfig:
        """Get the changeling-specific config from this agent.

        Raises RuntimeError if the agent config is not a ClaudeChangelingConfig,
        which indicates a misconfiguration in the agent type registration.
        """
        if not isinstance(self.agent_config, ClaudeChangelingConfig):
            raise RuntimeError(
                f"ClaudeChangelingAgent requires ClaudeChangelingConfig, got {type(self.agent_config).__name__}. "
                "This indicates the agent type was registered with the wrong config class."
            )
        return self.agent_config

    def _configure_role_hooks(
        self,
        host: OnlineHostInterface,
        active_role: str,
        role_dir_abs: str,
    ) -> None:
        """Write all hooks (readiness + memory sync) to the active role's settings.local.json.

        Writes directly to <active_role>/.claude/settings.local.json because
        that is where the role's Claude Code configuration lives (Claude Code
        runs from within the role directory).
        """
        settings_path = self.work_dir / active_role / ".claude" / "settings.local.json"

        existing_settings: dict[str, Any] = {}
        try:
            content = host.read_text_file(settings_path)
            existing_settings = json.loads(content)
        except FileNotFoundError:
            pass

        # Merge readiness hooks
        readiness_config = build_readiness_hooks_config()
        merged = merge_hooks_config(existing_settings, readiness_config)
        if merged is not None:
            existing_settings = merged

        # Merge memory sync hooks
        memory_config = build_memory_sync_hooks_config(role_dir_abs)
        merged = merge_hooks_config(existing_settings, memory_config)
        if merged is not None:
            existing_settings = merged

        with log_span("Configuring hooks in {}", settings_path):
            host.write_text_file(settings_path, json.dumps(existing_settings, indent=2) + "\n")

    @staticmethod
    def _get_role_from_env(options: CreateAgentOptions) -> str:
        """Read the ROLE env var from the agent's environment options.

        The ROLE env var must be passed via ``--env ROLE=<role>`` when creating
        the agent. It determines which role directory Claude Code runs from.

        Raises RuntimeError if ROLE is not set.
        """
        for env_var in options.environment.env_vars:
            if env_var.key == "ROLE":
                return env_var.value
        raise RuntimeError(
            "ROLE environment variable is required for changeling agents. "
            "Pass --env ROLE=<role> (e.g. --env ROLE=thinking) when creating the agent."
        )

    def modify_env_vars(
        self,
        host: OnlineHostInterface,
        env_vars: dict[str, str],
    ) -> None:
        """Set UV_TOOL_DIR, UV_TOOL_BIN_DIR, and prepend bin dir to PATH.

        These env vars ensure that ``uv tool install`` (run by the
        mng_recursive plugin) places the mng binary and its venv into
        the agent's state directory, and that any subsequent ``uv tool``
        invocations within the agent's processes also use the same paths.

        PATH is prepended so that ``mng`` is found directly by name.
        """
        agent_state_dir = env_vars.get("MNG_AGENT_STATE_DIR", "")
        if agent_state_dir:
            bin_dir = f"{agent_state_dir}/bin"
            env_vars["UV_TOOL_DIR"] = f"{agent_state_dir}/tools"
            env_vars["UV_TOOL_BIN_DIR"] = bin_dir

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Prepend ``cd "$ROLE" &&`` so Claude Code runs from within the role directory.

        The ``$ROLE`` env var is set via ``--env ROLE=<role>`` when the agent
        is created (passed by ``changeling deploy`` or manually by the user).

        This causes Claude Code to naturally discover ``.claude/``, skills,
        and ``CLAUDE.local.md`` from the role directory, while ``CLAUDE.md``
        at the repo root is found by walking up the directory tree.
        """
        base_command = super().assemble_command(host, agent_args, command_override)
        return CommandString(f'cd "$ROLE" && {base_command}')

    def provision(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mng_ctx: MngContext,
    ) -> None:
        """Provision a changeling role agent with llm toolchain and supporting service infrastructure.

        Extends ClaudeAgent provisioning with:
        0. Per-agent mng installation (via mng_recursive, before super())
        1. Settings loading from changelings.toml
        2. Talking role constraint validation (no skills or settings allowed)
        3. llm + plugin installation
        4. Default content (GLOBAL.md, role prompts, role .claude/ config)
        5. Symlinks (CLAUDE.md -> GLOBAL.md, <role>/CLAUDE.local.md -> <role>/PROMPT.md)
        6. All hooks (readiness + memory sync) written to <role>/.claude/settings.local.json
        7. Supporting service scripts and chat utilities
        8. Event log directory structure (events/<source>/events.jsonl)
        9. LLM tool scripts for conversation context
        10. Per-role memory directory setup
        """
        # Install mng before anything else so that supporting services
        # (event_watcher, web_server, etc.) can find it via UV_TOOL_BIN_DIR.
        # The env vars (UV_TOOL_DIR, UV_TOOL_BIN_DIR) are already written
        # to the agent env file by this point (host writes env before provision).
        from imbue.mng_recursive.provisioning import provision_mng_for_agent

        provision_mng_for_agent(agent=self, host=host, mng_ctx=mng_ctx)

        super().provision(host, options, mng_ctx)

        config = self._get_changeling_config()
        active_role = self._get_role_from_env(options)

        # Load settings from changelings.toml (falls back to defaults)
        settings = load_settings_from_host(host, self.work_dir)
        provisioning = settings.provisioning

        validate_talking_role_constraints(host, self.work_dir, provisioning)

        if config.install_llm:
            install_llm_toolchain(host, provisioning)

        provision_default_content(host, self.work_dir, provisioning)
        create_changeling_symlinks(host, self.work_dir, active_role, provisioning)

        # Write all hooks (readiness + memory sync) directly to the role's
        # settings.local.json. We write to the role directory because that's
        # where Claude Code runs and discovers its .claude/ config.
        work_dir_abs = resolve_work_dir_abs(host, self.work_dir, provisioning)
        role_dir_abs = f"{work_dir_abs}/{active_role}"
        self._configure_role_hooks(host, active_role, role_dir_abs)

        agent_state_dir = self._get_agent_dir()

        provision_supporting_services(host, agent_state_dir, provisioning)
        provision_llm_tools(host, agent_state_dir, provisioning)
        create_event_log_directories(host, agent_state_dir, provisioning)

        configure_llm_user_path(host, agent_state_dir, provisioning)
        create_changeling_conversations_table(host, agent_state_dir, provisioning)

        if config.install_llm:
            create_system_notifications_conversation(host, agent_state_dir, provisioning)
            create_slack_notifications_conversation(host, agent_state_dir, provisioning)
            chat_model = settings.chat.model or "claude-opus-4.6"
            create_daily_conversation(host, agent_state_dir, provisioning, chat_model)

        setup_memory_directory(host, self.work_dir, active_role, role_dir_abs, provisioning)


def inject_supporting_services(params: dict[str, Any]) -> None:
    """Inject all changeling supporting service tmux windows into the create command parameters.

    Adds:
    - Agent ttyd (web terminal for the primary role agent's tmux session)
    - Conversation watcher (syncs llm DB to JSONL files)
    - Event watcher (sends new events to primary role agent via mng message)
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
def register_cli_commands() -> Sequence[click.Command] | None:
    """Register changeling supporting service commands with mng."""
    from imbue.mng_claude_changeling.cli import get_all_commands

    return get_all_commands()


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface], type[AgentTypeConfig]]:
    """Register the claude-changeling agent type."""
    return ("claude-changeling", ClaudeChangelingAgent, ClaudeChangelingConfig)


def _is_claude_changeling_agent_type(agent_type_name: str) -> bool:
    """Check whether the given agent type name resolves to a ClaudeChangelingAgent subclass."""
    try:
        agent_class = get_agent_class(agent_type_name)
    except MngError as e:
        logger.debug("Could not resolve agent type '{}': {}", agent_type_name, e)
        return False
    return issubclass(agent_class, ClaudeChangelingAgent)


@hookimpl
def override_command_options(
    command_name: str,
    command_class: type,
    params: dict[str, Any],
) -> None:
    """Add changeling supporting service windows when creating claude-changeling role agents (or subtypes).

    Injects: agent ttyd, conversation watcher, event watcher, web server,
    and chat ttyd as supporting services.

    Matches any agent type whose registered class is ClaudeChangelingAgent or
    a subclass of it (e.g. elena-code, custom changeling types).
    """
    if command_name != "create":
        return

    agent_type = get_agent_type_from_params(params)
    if agent_type is None:
        return

    if not _is_claude_changeling_agent_type(agent_type):
        return

    inject_supporting_services(params)
