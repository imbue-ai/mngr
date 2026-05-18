"""Interactive urwid-based agent selector TUI.

Extracted out of ``cli/connect.py`` so that ``cli/agent_utils.py`` can use
it without creating an import cycle with the connect command module.
"""

import time
from typing import Any

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
from imbue.mngr.cli.urwid_utils import create_urwid_screen_preserving_terminal
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentLifecycleState


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
