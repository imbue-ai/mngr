from typing import Any

import click
from click_option_group import optgroup
from loguru import logger
from pydantic import ConfigDict
from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.event_loop.main_loop import MainLoop
from urwid.widget.attr_map import AttrMap
from urwid.widget.divider import Divider
from urwid.widget.frame import Frame
from urwid.widget.listbox import ListBox
from urwid.widget.listbox import SimpleFocusListWalker
from urwid.widget.pile import Pile
from urwid.widget.text import Text
from urwid.widget.wimp import SelectableIcon

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mng.cli.agent_utils import find_agent_for_command
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.urwid_utils import create_urwid_screen_preserving_terminal
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_mind_chat.api import ChatCommandError
from imbue.mng_mind_chat.api import ConversationInfo
from imbue.mng_mind_chat.api import get_latest_conversation_id
from imbue.mng_mind_chat.api import list_conversations_on_agent
from imbue.mng_mind_chat.api import run_chat_on_agent

MIND_LABEL_KEY = "mind"
MIND_LABEL_VALUE = "true"


class ChatCliOptions(CommonCliOptions):
    """Options passed from the CLI to the chat command."""

    agent: str | None
    new: bool
    last: bool
    conversation: str | None
    name: str | None
    start: bool
    allow_unknown_host: bool


class ConversationSelectorState(MutableModel):
    """Mutable state for the conversation selector UI."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    conversations: list[ConversationInfo]
    list_walker: Any
    result: ConversationInfo | None = None
    is_new_selected: bool = False


def _create_selectable_conversation_item(  # pragma: no cover
    conversation: ConversationInfo,
    name_width: int,
    model_width: int,
) -> AttrMap:
    """Create a selectable list item representing a conversation."""
    name = conversation.name or conversation.conversation_id
    name_padded = name.ljust(name_width)
    model_padded = conversation.model.ljust(model_width)
    display_text = f"{name_padded}  {model_padded}  {conversation.updated_at}"
    selectable_item = SelectableIcon(display_text, cursor_position=0)
    return AttrMap(selectable_item, None, focus_map="reversed")


def _handle_conversation_selector_input(  # pragma: no cover
    state: ConversationSelectorState,
    key: str,
) -> bool:
    """Handle keyboard input for the conversation selector."""
    if key == "ctrl c":
        raise ExitMainLoop()

    if key == "enter":
        if state.list_walker:
            _, focus_index = state.list_walker.get_focus()
            if focus_index is not None:
                # Last entry is "[New conversation]"
                if focus_index == len(state.conversations):
                    state.is_new_selected = True
                elif focus_index < len(state.conversations):
                    state.result = state.conversations[focus_index]
        raise ExitMainLoop()

    # Let arrow keys pass through to the ListBox for navigation
    if key in ("up", "down", "page up", "page down", "home", "end"):
        return False

    return False


class ConversationSelectorInputHandler(MutableModel):
    """Callable input handler for urwid MainLoop."""

    state: ConversationSelectorState

    def __call__(self, key: str | tuple[str, int, int, int]) -> bool | None:  # pragma: no cover
        if isinstance(key, tuple):
            return None
        handled = _handle_conversation_selector_input(self.state, key)
        return True if handled else None


def _run_conversation_selector(  # pragma: no cover
    conversations: list[ConversationInfo],
) -> tuple[ConversationInfo | None, bool]:
    """Run the conversation selector UI.

    Returns (selected_conversation, is_new_requested).
    """
    name_width = max((len(c.name or c.conversation_id) for c in conversations), default=10)
    model_width = max((len(c.model) for c in conversations), default=10)

    name_width = min(name_width, 50)
    model_width = min(model_width, 25)

    list_walker: SimpleFocusListWalker[AttrMap] = SimpleFocusListWalker([])

    for conversation in conversations:
        list_walker.append(_create_selectable_conversation_item(conversation, name_width, model_width))

    # Add "[New conversation]" at the bottom
    new_conv_text = "[New conversation]"
    new_conv_item = SelectableIcon(new_conv_text, cursor_position=0)
    list_walker.append(AttrMap(new_conv_item, None, focus_map="reversed"))

    list_walker.set_focus(0)

    listbox = ListBox(list_walker)

    state = ConversationSelectorState(
        conversations=conversations,
        list_walker=list_walker,
    )

    instructions_text = "Instructions:\n  Up/Down - Navigate the list\n  Enter - Select\n  Ctrl+C - Cancel"
    instructions = Text(instructions_text)

    header_text = f"{'NAME'.ljust(name_width)}  {'MODEL'.ljust(model_width)}  UPDATED"
    header_row = AttrMap(Text(("table_header", header_text)), "table_header")

    header = Pile(
        [
            AttrMap(Text("Conversation Selector", align="center"), "header"),
            Divider(),
            instructions,
            Divider(),
            header_row,
            Divider("-"),
        ]
    )

    frame = Frame(
        body=listbox,
        header=header,
    )

    palette = [
        ("header", "white", "dark blue"),
        ("reversed", "standout", ""),
        ("table_header", "bold", ""),
    ]

    input_handler = ConversationSelectorInputHandler(state=state)

    with create_urwid_screen_preserving_terminal() as screen:
        loop = MainLoop(
            frame,
            palette=palette,
            unhandled_input=input_handler,
            screen=screen,
        )
        loop.run()

    return state.result, state.is_new_selected


def _select_conversation_interactively(  # pragma: no cover
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> tuple[str | None, bool]:
    """Show an interactive conversation selector.

    Returns (conversation_id, is_new_requested).
    If conversation_id is None and is_new_requested is False, the user cancelled.
    """
    try:
        conversations = list_conversations_on_agent(agent, host)
    except ChatCommandError as e:
        logger.warning("Could not list conversations: {}", e)
        logger.info("Starting a new conversation instead.")
        return None, True

    if not conversations:
        logger.info("No conversations found. Starting a new one.")
        return None, True

    selected, is_new_requested = _run_conversation_selector(conversations)

    if is_new_requested:
        return None, True

    if selected is not None:
        return selected.conversation_id, False

    return None, False


def resolve_chat_args(
    opts: ChatCliOptions,
    agent: AgentInterface,
    host: OnlineHostInterface,
    is_interactive: bool,
) -> list[str] | None:
    """Determine the chat.sh arguments from CLI options and agent state.

    Returns the args list, or None if the user cancelled interactive selection.
    """
    # Validate mutually exclusive options
    exclusive_count = sum([opts.new, opts.last, opts.conversation is not None])
    if exclusive_count > 1:
        raise UserInputError("Only one of --new, --last, or --conversation can be specified")

    if opts.new:
        name = opts.name or "new conversation"
        return ["--new", "--name", name]
    elif opts.last:
        return _resolve_latest_conversation_args(agent, host)
    elif opts.conversation is not None:
        return ["--resume", opts.conversation]
    elif is_interactive:
        return _resolve_interactive_chat_args(agent, host)
    else:
        return _resolve_latest_conversation_args(agent, host)


def _resolve_interactive_chat_args(  # pragma: no cover
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> list[str] | None:
    """Show the interactive conversation selector and return chat args.

    Returns the args list, or None if the user cancelled.
    """
    conversation_id, is_new_requested = _select_conversation_interactively(agent, host)
    if is_new_requested:
        try:
            name = input("Conversation name [new conversation]: ").strip() or "new conversation"
        except (EOFError, KeyboardInterrupt):
            return None
        return ["--new", "--name", name]
    elif conversation_id is not None:
        return ["--resume", conversation_id]
    else:
        return None


def _resolve_latest_conversation_args(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> list[str]:
    """Resolve chat args for --last mode (or non-interactive default)."""
    try:
        latest_conversation_id = get_latest_conversation_id(agent, host)
    except ChatCommandError as e:
        logger.warning("Could not list conversations: {}", e)
        latest_conversation_id = None
    if latest_conversation_id is None:
        logger.info("No existing conversations found. Starting a new one.")
        return ["--new", "--name", "new conversation"]
    else:
        logger.info("Resuming latest conversation: {}", latest_conversation_id)
        return ["--resume", latest_conversation_id]


@pure
def _is_mind(labels: dict[str, str]) -> bool:
    """Check if an agent's labels indicate it is a mind."""
    return labels.get(MIND_LABEL_KEY) == MIND_LABEL_VALUE


@click.command()
@click.argument("agent", default=None, required=False)
@optgroup.group("General")
@optgroup.option("--agent", "agent", help="The agent to chat with (by name or ID)")
@optgroup.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Automatically start the agent if stopped",
)
@optgroup.group("Chat Options")
@optgroup.option(
    "--new",
    is_flag=True,
    default=False,
    help="Start a new conversation",
)
@optgroup.option(
    "--last",
    is_flag=True,
    default=False,
    help="Resume the most recently updated conversation",
)
@optgroup.option(
    "--conversation",
    help="Resume a specific conversation by ID",
)
@optgroup.option(
    "--name",
    help="Name for the conversation (used with --new)",
)
@optgroup.group("SSH Options")
@optgroup.option(
    "--allow-unknown-host/--no-allow-unknown-host",
    "allow_unknown_host",
    default=False,
    show_default=True,
    help="Allow connecting to hosts without a known_hosts file (disables SSH host key verification)",
)
@add_common_options
@click.pass_context
def chat(ctx: click.Context, **kwargs: Any) -> None:  # pragma: no cover
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="chat",
        command_class=ChatCliOptions,
    )

    # Find a mind agent.
    # When an agent is specified by name/ID, find it and validate its label.
    # When interactive with no agent specified, filter the selector to minds only.
    result = find_agent_for_command(
        mng_ctx=mng_ctx,
        agent_identifier=opts.agent,
        command_usage="chat <agent>",
        host_filter=None,
        is_start_desired=opts.start,
        agent_filter=lambda a: _is_mind(a.labels),
        no_agents_message="No mind agents found",
    )
    if result is None:
        logger.info("No agent selected")
        return
    agent, host = result

    # Validate mind label when agent was specified by name/ID
    # (the agent_filter only applies to interactive selection)
    if opts.agent is not None and not _is_mind(agent.get_labels()):
        raise UserInputError(
            f"Agent '{agent.name}' is not a mind and does not support chat. "
            f"Only agents with the label {MIND_LABEL_KEY}={MIND_LABEL_VALUE} "
            f"can be chatted with."
        )

    # Determine chat mode and build args
    chat_args = resolve_chat_args(opts, agent, host, is_interactive=mng_ctx.is_interactive)
    if chat_args is None:
        logger.info("No conversation selected")
        return

    logger.info("Connecting to chat for agent: {}", agent.name)
    run_chat_on_agent(
        agent=agent,
        host=host,
        mng_ctx=mng_ctx,
        chat_args=chat_args,
        is_unknown_host_allowed=opts.allow_unknown_host,
    )


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="chat",
    one_line_description="Chat with a mind agent",
    synopsis="mng chat [OPTIONS] [AGENT]",
    description="""Opens an interactive chat session with a mind agent's conversation
system. This connects to the agent's chat.sh script, which manages
conversations backed by the llm CLI tool.

If no agent is specified, shows an interactive selector to choose from
available agents.

If no conversation option is specified (--new, --last, or --conversation),
shows an interactive selector to choose from existing conversations or
start a new one.

The agent can be specified as a positional argument or via --agent:
  mng chat my-agent
  mng chat --agent my-agent""",
    examples=(
        ("Start a new named conversation", 'mng chat my-agent --new --name "Bug triage"'),
        ("Resume the most recent conversation", "mng chat my-agent --last"),
        ("Resume a specific conversation", "mng chat my-agent --conversation conv-1234567890-abcdef"),
        ("Show interactive agent selector", "mng chat"),
        ("Show interactive conversation selector", "mng chat my-agent"),
    ),
    see_also=(
        ("connect", "Connect to an agent's tmux session"),
        ("message", "Send a message to an agent"),
        ("exec", "Execute a command on an agent's host"),
    ),
).register()

add_pager_help_option(chat)
