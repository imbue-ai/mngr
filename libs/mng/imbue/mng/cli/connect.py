import time
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
from imbue.mng.api.connect import connect_to_agent
from imbue.mng.api.data_types import ConnectionOptions
from imbue.mng.api.discover import discover_all_hosts_and_agents
from imbue.mng.api.find import find_and_maybe_start_agent_by_name_or_id
from imbue.mng.api.list import list_agents
from imbue.mng.cli.agent_addr import find_agent_by_address
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.urwid_utils import create_urwid_screen_preserving_terminal
from imbue.mng.config.data_types import CommonCliOptions
from imbue.mng.errors import UserInputError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.interfaces.host import DEFAULT_AGENT_READY_TIMEOUT_SECONDS
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentLifecycleState


class ConnectCliOptions(CommonCliOptions):
    """Options passed from the CLI to the connect command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.
    """

    agent: str | None
    start: bool
    reconnect: bool
    message: str | None
    message_file: str | None
    ready_timeout: float
    retry: int
    retry_delay: str
    attach_command: str | None
    allow_unknown_host: bool


@pure
def filter_agents(
    agents: list[AgentDetails],
    hide_stopped: bool,
    search_query: str,
) -> list[AgentDetails]:
    """Filter agents by stopped state and search query."""
    result = agents

    if hide_stopped:
        result = [a for a in result if a.state != AgentLifecycleState.STOPPED]

    if search_query:
        query_lower = search_query.lower()
        result = [a for a in result if query_lower in str(a.name).lower()]

    return result


def build_status_text(
    search_query: str,
    hide_stopped: bool,
) -> str:
    """Build the status bar text for the agent selector."""
    parts = ["Status: Ready"]

    if search_query:
        parts.append(f"Search: {search_query}")
    else:
        parts.append("Type to search")

    if hide_stopped:
        parts.append("Filter: Hiding stopped")
    else:
        parts.append("Filter: All agents")

    return " | ".join(parts)


def handle_search_key(
    key: str,
    is_printable: bool,
    character: str | None,
    current_query: str,
) -> tuple[str, bool]:
    """Handle a key press for typeahead search. Returns (new_query, should_refresh)."""
    if key == "backspace":
        if current_query:
            return current_query[:-1], True
        else:
            return current_query, False
    elif is_printable and character:
        return current_query + character, True
    else:
        return current_query, False


def _create_selectable_agent_item(agent: AgentDetails, name_width: int, state_width: int) -> AttrMap:
    """Create a selectable list item representing an agent as a table row.

    Uses SelectableIcon instead of Text so that ListBox can navigate between items.
    urwid.Text is not selectable, which prevents ListBox arrow key navigation.
    """
    # Pad the name and state to their column widths for proper alignment
    name_padded = str(agent.name).ljust(name_width)
    state_padded = agent.state.value.ljust(state_width)
    host_str = str(agent.host.name)

    # Create a single SelectableIcon with the full formatted row
    # This ensures the entire row is selectable as one unit
    display_text = f"{name_padded}  {state_padded}  {host_str}"
    selectable_item = SelectableIcon(display_text, cursor_position=0)

    return AttrMap(selectable_item, None, focus_map="reversed")


class AgentSelectorState(MutableModel):
    """Mutable state for the agent selector UI."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    agents: list[AgentDetails]
    filtered_agents: list[AgentDetails] = []
    list_walker: Any
    status_text: Any
    result: AgentDetails | None = None
    hide_stopped: bool = False
    search_query: str = ""
    last_ctrl_c_time: float = 0.0
    name_width: int = 0
    state_width: int = 0


def _refresh_agent_list(state: AgentSelectorState) -> None:
    """Refresh the agent list view with current filter settings."""
    state.filtered_agents = filter_agents(state.agents, state.hide_stopped, state.search_query)

    state.list_walker.clear()
    for agent in state.filtered_agents:
        state.list_walker.append(_create_selectable_agent_item(agent, state.name_width, state.state_width))

    if state.list_walker:
        state.list_walker.set_focus(0)

    state.status_text.set_text(build_status_text(state.search_query, state.hide_stopped))


def _handle_selector_input(state: AgentSelectorState, key: str) -> bool:
    """Handle keyboard input for the agent selector. Returns True if handled, False to pass through."""
    if key == "ctrl r":
        state.hide_stopped = not state.hide_stopped
        _refresh_agent_list(state)
        return True

    if key == "ctrl c":
        current_time = time.time()
        if state.search_query:
            # First Ctrl-c clears the search query
            state.search_query = ""
            state.last_ctrl_c_time = current_time
            _refresh_agent_list(state)
            return True
        elif current_time - state.last_ctrl_c_time < 0.5:
            # Second Ctrl-c within 500ms exits
            raise ExitMainLoop()
        else:
            # Single Ctrl-c with no query - record time and wait for potential second
            state.last_ctrl_c_time = current_time
            return True

    if key == "enter":
        if state.list_walker and state.filtered_agents:
            _, focus_index = state.list_walker.get_focus()
            if focus_index is not None and 0 <= focus_index < len(state.filtered_agents):
                state.result = state.filtered_agents[focus_index]
        raise ExitMainLoop()

    # Let arrow keys pass through to the ListBox for navigation
    if key in ("up", "down", "page up", "page down", "home", "end"):
        return False

    is_printable = len(key) == 1 and key.isprintable()
    character = key if is_printable else None

    new_query, should_refresh = handle_search_key(
        key=key,
        is_printable=is_printable,
        character=character,
        current_query=state.search_query,
    )

    if should_refresh:
        state.search_query = new_query
        _refresh_agent_list(state)
        return True

    return False


class SelectorInputHandler(MutableModel):
    """Callable input handler for urwid MainLoop."""

    state: AgentSelectorState

    def __call__(self, key: str | tuple[str, int, int, int]) -> bool | None:
        """Handle keyboard input. Returns True if handled, None to pass through."""
        if isinstance(key, tuple):
            return None
        handled = _handle_selector_input(self.state, key)
        return True if handled else None


def _run_agent_selector(agents: list[AgentDetails]) -> AgentDetails | None:
    """Run the agent selector UI and return the selected agent, or None if cancelled."""
    # Calculate column widths based on content
    name_width = max((len(str(a.name)) for a in agents), default=10)
    state_width = max((len(a.state.value) for a in agents), default=7)

    # Cap widths at reasonable maximums
    name_width = min(name_width, 40)
    state_width = min(state_width, 15)

    list_walker: SimpleFocusListWalker[AttrMap] = SimpleFocusListWalker([])
    listbox = ListBox(list_walker)

    status_text = Text(build_status_text("", False))
    status_bar = AttrMap(status_text, "status")

    state = AgentSelectorState(
        agents=agents,
        list_walker=list_walker,
        status_text=status_text,
        name_width=name_width,
        state_width=state_width,
    )

    instructions_text = (
        "Instructions:\n"
        "  Type - Search agents by name\n"
        "  Up/Down - Navigate the list\n"
        "  Enter - Select an agent\n"
        "  Backspace - Clear search character\n"
        "  Ctrl+C - Clear search (twice to quit)\n"
        "  Ctrl+R - Toggle hiding stopped agents"
    )
    instructions = Text(instructions_text)

    # Create table header matching the SelectableIcon format in list items
    header_text = f"{'NAME'.ljust(name_width)}  {'STATE'.ljust(state_width)}  HOST"
    header_row = AttrMap(Text(("table_header", header_text)), "table_header")

    _refresh_agent_list(state)

    header = Pile(
        [
            AttrMap(Text("Agent Selector", align="center"), "header"),
            Divider(),
            instructions,
            Divider(),
            header_row,
            Divider("-"),
        ]
    )

    footer = Pile(
        [
            Divider(),
            status_bar,
        ]
    )

    frame = Frame(
        body=listbox,
        header=header,
        footer=footer,
    )

    palette = [
        ("header", "white", "dark blue"),
        ("status", "white", "dark blue"),
        ("reversed", "standout", ""),
        ("table_header", "bold", ""),
    ]

    input_handler = SelectorInputHandler(state=state)

    with create_urwid_screen_preserving_terminal() as screen:
        loop = MainLoop(
            frame,
            palette=palette,
            unhandled_input=input_handler,
            screen=screen,
        )
        loop.run()

    return state.result


def select_agent_interactively(agents: list[AgentDetails]) -> AgentDetails | None:
    """Show an interactive UI to select an agent. Returns None if cancelled."""
    if not agents:
        return None

    return _run_agent_selector(agents)


@pure
def _build_connection_options(opts: ConnectCliOptions) -> ConnectionOptions:
    """Build ConnectionOptions from CLI options."""
    return ConnectionOptions(
        is_reconnect=opts.reconnect,
        retry_count=opts.retry,
        retry_delay=opts.retry_delay,
        attach_command=opts.attach_command,
        is_unknown_host_allowed=opts.allow_unknown_host,
    )


@click.command()
@click.argument("agent", default=None, required=False)
@optgroup.group("General")
@optgroup.option("--agent", "agent", help="The agent to connect to (by name or ID)")
@optgroup.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Automatically start the agent if stopped",
)
@optgroup.group("Options")
@optgroup.option(
    "--reconnect/--no-reconnect",
    default=True,
    show_default=True,
    help="Automatically reconnect if dropped [future]",
)
@optgroup.option("--message", help="Initial message to send after connecting [future]")
@optgroup.option(
    "--message-file", type=click.Path(exists=True), help="File containing initial message to send [future]"
)
@optgroup.option(
    "--ready-timeout",
    type=float,
    default=DEFAULT_AGENT_READY_TIMEOUT_SECONDS,
    show_default=True,
    help="Timeout in seconds to wait for agent readiness [future]",
)
@optgroup.option("--retry", type=int, default=3, show_default=True, help="Number of connection retries [future]")
@optgroup.option("--retry-delay", default="5s", show_default=True, help="Delay between retries [future]")
@optgroup.option("--attach-command", help="Command to run instead of attaching to main session [future]")
@optgroup.option(
    "--allow-unknown-host/--no-allow-unknown-host",
    "allow_unknown_host",
    default=False,
    show_default=True,
    help="Allow connecting to hosts without a known_hosts file (disables SSH host key verification)",
)
@add_common_options
@click.pass_context
def connect(ctx: click.Context, **kwargs: Any) -> None:
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="connect",
        command_class=ConnectCliOptions,
    )

    # Send the specified text as an initial message after the agent starts
    # Should wait for ready_timeout seconds for agent readiness before sending
    if opts.message is not None:
        raise NotImplementedError("--message is not implemented yet")

    # Read initial message content from the specified file and send after agent starts
    if opts.message_file is not None:
        raise NotImplementedError("--message-file is not implemented yet")

    # Timeout for waiting for agent readiness
    if opts.ready_timeout != DEFAULT_AGENT_READY_TIMEOUT_SECONDS:
        raise NotImplementedError("--ready-timeout with non-default value is not implemented yet")

    # Number of times to retry connection on failure before giving up
    if opts.retry != 3:
        raise NotImplementedError("--retry with non-default value is not implemented yet")

    # Delay between connection retries (supports durations like "5s", "1m")
    if opts.retry_delay != "5s":
        raise NotImplementedError("--retry-delay with non-default value is not implemented yet")

    # Run this command instead of the default tmux attach
    # Useful for running a different shell or command in the agent's environment
    if opts.attach_command is not None:
        raise NotImplementedError("--attach-command is not implemented yet")

    # Disable automatic reconnection if the connection is dropped
    # Default behavior (--reconnect) should automatically reconnect
    if not opts.reconnect:
        raise NotImplementedError("--no-reconnect is not implemented yet")

    logger.info("Finding agent...")
    agents_by_host, providers = discover_all_hosts_and_agents(mng_ctx)

    agent: AgentInterface
    host: OnlineHostInterface

    if opts.agent is not None:
        agent, host = find_agent_by_address(
            opts.agent,
            agents_by_host,
            mng_ctx,
            "connect",
            is_start_desired=opts.start,
        )
    elif not mng_ctx.is_interactive:
        # Default to most recently created agent when running non-interactively
        list_result = list_agents(mng_ctx, is_streaming=False)
        if not list_result.agents:
            raise UserInputError("No agents found")

        # Sort by create_time descending to get most recent first
        sorted_agents = sorted(list_result.agents, key=lambda a: a.create_time, reverse=True)
        most_recent = sorted_agents[0]
        logger.info("No agent specified, connecting to most recently created: {}", most_recent.name)
        agent, host = find_and_maybe_start_agent_by_name_or_id(
            str(most_recent.id),
            agents_by_host,
            mng_ctx,
            "connect",
            is_start_desired=opts.start,
        )
    else:
        list_result = list_agents(mng_ctx, is_streaming=False)
        if not list_result.agents:
            raise UserInputError("No agents found")

        selected = select_agent_interactively(list_result.agents)
        if selected is None:
            logger.info("No agent selected")
            return

        agent, host = find_and_maybe_start_agent_by_name_or_id(
            str(selected.id),
            agents_by_host,
            mng_ctx,
            "connect",
            is_start_desired=opts.start,
        )

    # Build connection options
    connection_opts = _build_connection_options(opts)

    logger.info("Connecting to agent: {}", agent.name)
    connect_to_agent(agent, host, mng_ctx, connection_opts)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="connect",
    one_line_description="Connect to an existing agent via the terminal",
    synopsis="mng [connect|conn] [OPTIONS] [AGENT]",
    description="""Attaches to the agent's tmux session, roughly equivalent to SSH'ing into
the agent's machine and attaching to the tmux session.

If no agent is specified, shows an interactive selector to choose from
available agents. The selector allows typeahead search to filter agents
by name.

The agent can be specified as a positional argument or via --agent:
  mng connect my-agent
  mng connect --agent my-agent""",
    aliases=("conn",),
    examples=(
        ("Connect to an agent by name", "mng connect my-agent"),
        ("Connect without auto-starting if stopped", "mng connect my-agent --no-start"),
        ("Show interactive agent selector", "mng connect"),
    ),
    see_also=(
        ("create", "Create and connect to a new agent"),
        ("list", "List available agents"),
    ),
).register()

# Add pager-enabled help option to the connect command
add_pager_help_option(connect)
