import json
import shlex
import sys
from typing import Any

import click
from click_option_group import optgroup
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from urwid.display.raw import Screen
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

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mng.cli.agent_utils import find_agent_for_command
from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_changeling_chat.api import get_latest_conversation_id
from imbue.mng_changeling_chat.api import run_chat_on_agent


class ChatCliOptions(CommonCliOptions):
    """Options passed from the CLI to the chat command."""

    agent: str | None
    new: bool
    last: bool
    conversation: str | None
    start: bool
    allow_unknown_host: bool


class ConversationInfo(FrozenModel):
    """Information about a conversation for the interactive selector."""

    conversation_id: str = Field(description="Unique conversation identifier")
    model: str = Field(description="Model used for this conversation")
    created_at: str = Field(description="When the conversation was created")
    updated_at: str = Field(description="When the conversation was last updated")


class ConversationSelectorState(MutableModel):
    """Mutable state for the conversation selector UI."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    conversations: list[ConversationInfo]
    list_walker: Any
    result: ConversationInfo | None = None
    is_new_selected: bool = False


def _create_selectable_conversation_item(
    conversation: ConversationInfo,
    cid_width: int,
    model_width: int,
) -> AttrMap:
    """Create a selectable list item representing a conversation."""
    cid_padded = conversation.conversation_id.ljust(cid_width)
    model_padded = conversation.model.ljust(model_width)
    display_text = f"{cid_padded}  {model_padded}  {conversation.updated_at}"
    selectable_item = SelectableIcon(display_text, cursor_position=0)
    return AttrMap(selectable_item, None, focus_map="reversed")


def _handle_conversation_selector_input(
    state: ConversationSelectorState,
    key: str,
) -> bool:
    """Handle keyboard input for the conversation selector."""
    if key == "ctrl c":
        raise ExitMainLoop()

    if key == "n":
        state.is_new_selected = True
        raise ExitMainLoop()

    if key == "enter":
        if state.list_walker and state.conversations:
            _, focus_index = state.list_walker.get_focus()
            if focus_index is not None and 0 <= focus_index < len(state.conversations):
                state.result = state.conversations[focus_index]
        raise ExitMainLoop()

    # Let arrow keys pass through to the ListBox for navigation
    if key in ("up", "down", "page up", "page down", "home", "end"):
        return False

    return False


class ConversationSelectorInputHandler(MutableModel):
    """Callable input handler for urwid MainLoop."""

    state: ConversationSelectorState

    def __call__(self, key: str | tuple[str, int, int, int]) -> bool | None:
        if isinstance(key, tuple):
            return None
        handled = _handle_conversation_selector_input(self.state, key)
        return True if handled else None


def _run_conversation_selector(
    conversations: list[ConversationInfo],
) -> tuple[ConversationInfo | None, bool]:
    """Run the conversation selector UI.

    Returns (selected_conversation, is_new_requested).
    """
    cid_width = max((len(c.conversation_id) for c in conversations), default=10)
    model_width = max((len(c.model) for c in conversations), default=10)

    cid_width = min(cid_width, 50)
    model_width = min(model_width, 25)

    list_walker: SimpleFocusListWalker[AttrMap] = SimpleFocusListWalker([])
    for conversation in conversations:
        list_walker.append(_create_selectable_conversation_item(conversation, cid_width, model_width))

    if list_walker:
        list_walker.set_focus(0)

    listbox = ListBox(list_walker)

    state = ConversationSelectorState(
        conversations=conversations,
        list_walker=list_walker,
    )

    instructions_text = (
        "Instructions:\n"
        "  Up/Down - Navigate the list\n"
        "  Enter - Resume selected conversation\n"
        "  n - Start a new conversation\n"
        "  Ctrl+C - Cancel"
    )
    instructions = Text(instructions_text)

    header_text = f"{'CONVERSATION'.ljust(cid_width)}  {'MODEL'.ljust(model_width)}  UPDATED"
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

    screen = Screen()
    screen.tty_signal_keys(intr="undefined")

    loop = MainLoop(
        frame,
        palette=palette,
        unhandled_input=input_handler,
        screen=screen,
    )
    loop.run()

    return state.result, state.is_new_selected


def _list_conversations_on_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> list[ConversationInfo]:
    """List conversations for an agent by reading event files on the host."""
    agent_state_dir = host.host_dir / "agents" / str(agent.id)
    conversations_events_path = agent_state_dir / "events" / "conversations" / "events.jsonl"
    messages_events_path = agent_state_dir / "events" / "messages" / "events.jsonl"

    # Build a Python script to read conversations and return JSON
    read_script = f"""
import json, sys
from pathlib import Path

conv_file = Path('{conversations_events_path}')
msg_file = Path('{messages_events_path}')

if not conv_file.exists():
    print('[]')
    sys.exit(0)

convs = {{}}
for line in conv_file.read_text().splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        event = json.loads(line)
        cid = event['conversation_id']
        convs[cid] = event
    except (json.JSONDecodeError, KeyError):
        continue

if not convs:
    print('[]')
    sys.exit(0)

updated_at = {{}}
for cid, event in convs.items():
    updated_at[cid] = event.get('timestamp', '')

if msg_file.exists():
    for line in msg_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            cid = msg.get('conversation_id', '')
            ts = msg.get('timestamp', '')
            if cid in convs and ts:
                if cid not in updated_at or ts > updated_at[cid]:
                    updated_at[cid] = ts
        except (json.JSONDecodeError, KeyError):
            continue

result = []
for cid, event in convs.items():
    result.append({{
        'conversation_id': cid,
        'model': event.get('model', '?'),
        'created_at': event.get('timestamp', '?'),
        'updated_at': updated_at.get(cid, event.get('timestamp', '?')),
    }})

result.sort(key=lambda r: r['updated_at'], reverse=True)
print(json.dumps(result))
"""

    result = host.execute_command(
        f"python3 -c {shlex.quote(read_script)}",
        cwd=agent.work_dir,
    )

    if not result.success:
        logger.debug("Failed to list conversations: {}", result.stderr)
        return []

    try:
        raw_conversations = json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        logger.debug("Failed to parse conversation list output: {}", result.stdout)
        return []

    return [ConversationInfo.model_validate(conv) for conv in raw_conversations]


def _select_conversation_interactively(
    agent: AgentInterface,
    host: OnlineHostInterface,
) -> tuple[str | None, bool]:
    """Show an interactive conversation selector.

    Returns (conversation_id, is_new_requested).
    If conversation_id is None and is_new_requested is False, the user cancelled.
    """
    conversations = _list_conversations_on_agent(agent, host)

    if not conversations:
        logger.info("No conversations found. Starting a new one.")
        return None, True

    selected, is_new_requested = _run_conversation_selector(conversations)

    if is_new_requested:
        return None, True

    if selected is not None:
        return selected.conversation_id, False

    return None, False


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
def chat(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="chat",
        command_class=ChatCliOptions,
    )

    # Validate mutually exclusive options
    exclusive_count = sum([opts.new, opts.last, opts.conversation is not None])
    if exclusive_count > 1:
        raise UserInputError("Only one of --new, --last, or --conversation can be specified")

    # Find the agent
    result = find_agent_for_command(
        mng_ctx=mng_ctx,
        agent_identifier=opts.agent,
        command_usage="chat <agent>",
        host_filter=None,
        is_start_desired=opts.start,
    )
    if result is None:
        logger.info("No agent selected")
        return
    agent, host = result

    # Determine chat mode and build args
    chat_args: list[str]

    if opts.new:
        chat_args = ["--new"]
    elif opts.last:
        # Find the latest conversation
        latest_cid = get_latest_conversation_id(agent, host)
        if latest_cid is None:
            logger.info("No existing conversations found. Starting a new one.")
            chat_args = ["--new"]
        else:
            logger.info("Resuming latest conversation: {}", latest_cid)
            chat_args = ["--resume", latest_cid]
    elif opts.conversation is not None:
        chat_args = ["--resume", opts.conversation]
    elif sys.stdin.isatty():
        # Interactive mode: show conversation selector
        conversation_id, is_new_requested = _select_conversation_interactively(agent, host)
        if is_new_requested:
            chat_args = ["--new"]
        elif conversation_id is not None:
            chat_args = ["--resume", conversation_id]
        else:
            logger.info("No conversation selected")
            return
    else:
        # Non-interactive: default to --last behavior
        latest_cid = get_latest_conversation_id(agent, host)
        if latest_cid is None:
            chat_args = ["--new"]
        else:
            chat_args = ["--resume", latest_cid]

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
    one_line_description="Chat with a changeling agent",
    synopsis="mng chat [OPTIONS] [AGENT]",
    description="""Opens an interactive chat session with a changeling agent's conversation
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
        ("Start a new conversation with an agent", "mng chat my-agent --new"),
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
