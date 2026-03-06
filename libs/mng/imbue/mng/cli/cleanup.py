import time
from typing import Any
from typing import assert_never

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
from imbue.mng.api.cleanup import execute_cleanup
from imbue.mng.api.cleanup import find_agents_for_cleanup
from imbue.mng.api.data_types import CleanupResult
from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.connect import build_status_text
from imbue.mng.cli.connect import filter_agents
from imbue.mng.cli.connect import handle_search_key
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.output_helpers import AbortError
from imbue.mng.cli.output_helpers import emit_event
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import emit_info
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.cli.urwid_utils import create_urwid_screen_preserving_terminal
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.primitives import CleanupAction
from imbue.mng.primitives import ErrorBehavior
from imbue.mng.primitives import OutputFormat
from imbue.mng.utils.duration import parse_duration_to_seconds


class CleanupCliOptions(CommonCliOptions):
    """Options passed from the CLI to the cleanup command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the cleanup() function itself.
    """

    force: bool
    dry_run: bool
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    older_than: str | None
    idle_for: str | None
    tag: tuple[str, ...]
    provider: tuple[str, ...]
    agent_type: tuple[str, ...]
    action: str
    snapshot_before: bool


@click.command(name="cleanup")
@optgroup.group("General")
@optgroup.option(
    "-f",
    "--force",
    "--yes",
    is_flag=True,
    help="Skip confirmation prompts",
)
@optgroup.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be destroyed or stopped without executing",
)
@optgroup.group("Filtering")
@optgroup.option(
    "--include",
    multiple=True,
    help="Include only agents matching this CEL filter (repeatable)",
)
@optgroup.option(
    "--exclude",
    multiple=True,
    help="Exclude agents matching this CEL filter (repeatable)",
)
@optgroup.option(
    "--older-than",
    default=None,
    help="Select agents older than specified duration (e.g., 7d, 24h)",
)
@optgroup.option(
    "--idle-for",
    default=None,
    help="Select agents idle for at least this duration (e.g., 1h, 30m)",
)
@optgroup.option(
    "--tag",
    multiple=True,
    help="Select agents/hosts with this tag (repeatable)",
)
@optgroup.option(
    "--provider",
    multiple=True,
    help="Select hosts from this provider (repeatable)",
)
@optgroup.option(
    "--agent-type",
    multiple=True,
    help="Select this agent type, e.g., claude, codex (repeatable)",
)
@optgroup.group("Actions")
@optgroup.option(
    "--action",
    type=click.Choice(["destroy", "stop"], case_sensitive=False),
    default="destroy",
    show_default=True,
    help="Action to perform on selected agents",
)
@optgroup.option(
    "--destroy",
    "action",
    flag_value="destroy",
    help="Destroy selected agents/hosts (default)",
)
@optgroup.option(
    "--stop",
    "action",
    flag_value="stop",
    help="Stop selected agents instead of destroying",
)
@optgroup.option(
    "--snapshot-before",
    is_flag=True,
    help="Create snapshots before destroying or stopping [future]",
)
@add_common_options
@click.pass_context
def cleanup(ctx: click.Context, **kwargs) -> None:
    try:
        _cleanup_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _cleanup_impl(ctx: click.Context, **kwargs) -> None:
    """Implementation of the cleanup command."""
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="cleanup",
        command_class=CleanupCliOptions,
    )
    logger.debug("Started cleanup command")

    # --snapshot-before is a future feature
    if opts.snapshot_before:
        raise NotImplementedError("The --snapshot-before option is not yet implemented.")

    # Resolve the action
    action = CleanupAction(opts.action.upper())
    error_behavior = ErrorBehavior.CONTINUE

    # Build CEL filters from convenience options
    include_filters, exclude_filters = _build_cel_filters_from_options(opts)

    # Find agents matching the filters
    emit_info("Finding agents...", output_opts.output_format)
    agents = find_agents_for_cleanup(
        mng_ctx=mng_ctx,
        include_filters=tuple(include_filters),
        exclude_filters=tuple(exclude_filters),
        error_behavior=error_behavior,
    )

    if not agents:
        _emit_no_agents_found(output_opts)
        return

    # Interactive selection or non-interactive path
    if mng_ctx.is_interactive and not opts.force and not opts.dry_run:
        selected_agents = _run_interactive_selection(agents, action)
        if not selected_agents:
            emit_info("No agents selected.", output_opts.output_format)
            return
    else:
        selected_agents = agents

    # Dry run: just show what would happen
    if opts.dry_run:
        _emit_dry_run_output(selected_agents, action, output_opts)
        return

    # Execute the cleanup action
    match action:
        case CleanupAction.DESTROY:
            action_label = "Destroying"
        case CleanupAction.STOP:
            action_label = "Stopping"
        case _ as unreachable:
            assert_never(unreachable)
    emit_info(f"{action_label} {len(selected_agents)} agent(s)...", output_opts.output_format)

    result = execute_cleanup(
        mng_ctx=mng_ctx,
        agents=selected_agents,
        action=action,
        is_dry_run=False,
        error_behavior=error_behavior,
    )

    # Output results
    _emit_result(result, output_opts)


@pure
def _build_cel_filters_from_options(
    opts: CleanupCliOptions,
) -> tuple[list[str], list[str]]:
    """Build CEL include/exclude filters from convenience CLI options."""
    include_filters = list(opts.include)
    exclude_filters = list(opts.exclude)

    # --older-than DURATION -> age > N (seconds)
    if opts.older_than is not None:
        older_than_seconds = parse_duration_to_seconds(opts.older_than)
        include_filters.append(f"age > {older_than_seconds}")

    # --idle-for DURATION -> idle > N (seconds)
    if opts.idle_for is not None:
        idle_for_seconds = parse_duration_to_seconds(opts.idle_for)
        include_filters.append(f"idle > {idle_for_seconds}")

    # --provider PROVIDER -> host.provider == "PROVIDER" (repeatable, OR'd)
    if opts.provider:
        provider_conditions = [f'host.provider == "{p}"' for p in opts.provider]
        if len(provider_conditions) == 1:
            include_filters.append(provider_conditions[0])
        else:
            include_filters.append("(" + " || ".join(provider_conditions) + ")")

    # --agent-type TYPE -> type == "TYPE" (repeatable, OR'd)
    if opts.agent_type:
        type_conditions = [f'type == "{t}"' for t in opts.agent_type]
        if len(type_conditions) == 1:
            include_filters.append(type_conditions[0])
        else:
            include_filters.append("(" + " || ".join(type_conditions) + ")")

    # --tag TAG -> host.tags (repeatable)
    if opts.tag:
        for tag in opts.tag:
            if "=" in tag:
                key, value = tag.split("=", 1)
                include_filters.append(f'host.tags.{key} == "{value}"')
            else:
                include_filters.append(f'host.tags.{tag} == "true"')

    return include_filters, exclude_filters


def _run_interactive_selection(
    agents: list[AgentDetails],
    action: CleanupAction,
) -> list[AgentDetails]:
    """Show a urwid-based multi-select TUI for choosing agents to clean up."""
    if not agents:
        return []
    return _run_cleanup_selector(agents, action)


# =============================================================================
# Urwid multi-select TUI for cleanup
# =============================================================================


class _CleanupSelectorState(MutableModel):
    """Mutable state for the cleanup multi-select TUI."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    agents: list[AgentDetails]
    filtered_agents: list[AgentDetails] = []
    selected_ids: set[str] = set()
    list_walker: Any
    status_text: Any
    result: list[AgentDetails] | None = None
    hide_stopped: bool = False
    search_query: str = ""
    last_ctrl_c_time: float = 0.0
    name_width: int = 0
    state_width: int = 0
    provider_width: int = 0
    action: CleanupAction = CleanupAction.DESTROY


@pure
def _selected_marker(is_selected: bool) -> str:
    """Return [x] or [ ] depending on selection state."""
    return "[x]" if is_selected else "[ ]"


def _create_cleanup_list_item(
    agent: AgentDetails,
    is_selected: bool,
    name_width: int,
    state_width: int,
    provider_width: int,
) -> AttrMap:
    """Create a selectable list item for the cleanup selector."""
    marker = _selected_marker(is_selected)
    name_padded = str(agent.name).ljust(name_width)
    state_padded = agent.state.value.ljust(state_width)
    provider_padded = str(agent.host.provider_name).ljust(provider_width)
    host_str = str(agent.host.name)

    display_text = f"{marker} {name_padded}  {state_padded}  {provider_padded}  {host_str}"
    selectable_item = SelectableIcon(display_text, cursor_position=0)

    return AttrMap(selectable_item, None, focus_map="reversed")


def _build_cleanup_status_text(
    search_query: str,
    hide_stopped: bool,
    selected_count: int,
    total_count: int,
    action: CleanupAction,
) -> str:
    """Build the status bar text for the cleanup selector."""
    base_status = build_status_text(search_query, hide_stopped)
    match action:
        case CleanupAction.DESTROY:
            action_word = "destroy"
        case CleanupAction.STOP:
            action_word = "stop"
        case _ as unreachable:
            assert_never(unreachable)
    return f"{base_status} | Selected: {selected_count}/{total_count} to {action_word}"


def _refresh_cleanup_list(state: _CleanupSelectorState) -> None:
    """Refresh the agent list view with current filter and selection state."""
    state.filtered_agents = filter_agents(state.agents, state.hide_stopped, state.search_query)

    # Preserve focus position
    _, old_focus = state.list_walker.get_focus() if state.list_walker else (None, None)

    state.list_walker.clear()
    for agent in state.filtered_agents:
        is_selected = str(agent.id) in state.selected_ids
        state.list_walker.append(
            _create_cleanup_list_item(agent, is_selected, state.name_width, state.state_width, state.provider_width)
        )

    if state.list_walker:
        safe_focus = min(old_focus, len(state.list_walker) - 1) if old_focus is not None else 0
        state.list_walker.set_focus(max(safe_focus, 0))

    state.status_text.set_text(
        _build_cleanup_status_text(
            state.search_query, state.hide_stopped, len(state.selected_ids), len(state.agents), state.action
        )
    )


def _handle_cleanup_input(state: _CleanupSelectorState, key: str) -> bool:
    """Handle keyboard input for the cleanup selector. Returns True if handled."""
    # Space toggles selection on the focused item (safe since agent names cannot contain spaces)
    if key == " ":
        if state.list_walker and state.filtered_agents:
            _, focus_index = state.list_walker.get_focus()
            if focus_index is not None and 0 <= focus_index < len(state.filtered_agents):
                agent_id = str(state.filtered_agents[focus_index].id)
                if agent_id in state.selected_ids:
                    state.selected_ids.discard(agent_id)
                else:
                    state.selected_ids.add(agent_id)
                _refresh_cleanup_list(state)
        return True

    # Ctrl+A selects all visible agents
    if key == "ctrl a":
        for agent in state.filtered_agents:
            state.selected_ids.add(str(agent.id))
        _refresh_cleanup_list(state)
        return True

    # Ctrl+N deselects all
    if key == "ctrl n":
        state.selected_ids.clear()
        _refresh_cleanup_list(state)
        return True

    # Enter confirms selection
    if key == "enter":
        state.result = [agent for agent in state.agents if str(agent.id) in state.selected_ids]
        raise ExitMainLoop()

    # Ctrl+R toggles hiding stopped agents (reusing connect.py pattern)
    if key == "ctrl r":
        state.hide_stopped = not state.hide_stopped
        _refresh_cleanup_list(state)
        return True

    # Ctrl+C: clear search first, then quit on double-press
    if key == "ctrl c":
        current_time = time.time()
        if state.search_query:
            state.search_query = ""
            state.last_ctrl_c_time = current_time
            _refresh_cleanup_list(state)
            return True
        elif current_time - state.last_ctrl_c_time < 0.5:
            state.result = []
            raise ExitMainLoop()
        else:
            state.last_ctrl_c_time = current_time
            return True

    # Arrow keys pass through to ListBox for navigation
    if key in ("up", "down", "page up", "page down", "home", "end"):
        return False

    # Typeahead search (reusing connect.py pattern)
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
        _refresh_cleanup_list(state)
        return True

    return False


class _CleanupInputHandler(MutableModel):
    """Callable input handler for urwid MainLoop."""

    state: _CleanupSelectorState

    def __call__(self, key: str | tuple[str, int, int, int]) -> bool | None:
        if isinstance(key, tuple):
            return None
        handled = _handle_cleanup_input(self.state, key)
        return True if handled else None


def _run_cleanup_selector(agents: list[AgentDetails], action: CleanupAction) -> list[AgentDetails]:
    """Run the multi-select cleanup TUI and return selected agents."""
    # Calculate column widths
    name_width = min(max((len(str(a.name)) for a in agents), default=10), 40)
    state_width = min(max((len(a.state.value) for a in agents), default=7), 15)
    provider_width = min(max((len(str(a.host.provider_name)) for a in agents), default=5), 20)

    list_walker: SimpleFocusListWalker[AttrMap] = SimpleFocusListWalker([])
    listbox = ListBox(list_walker)

    status_text = Text("")
    status_bar = AttrMap(status_text, "status")

    state = _CleanupSelectorState(
        agents=agents,
        list_walker=list_walker,
        status_text=status_text,
        name_width=name_width,
        state_width=state_width,
        provider_width=provider_width,
        action=action,
    )

    match action:
        case CleanupAction.DESTROY:
            title_action = "Destroy"
        case CleanupAction.STOP:
            title_action = "Stop"
        case _ as unreachable:
            assert_never(unreachable)

    instructions_text = (
        "Instructions:\n"
        "  Space - Toggle selection on focused agent\n"
        "  Ctrl+A - Select all visible agents\n"
        "  Ctrl+N - Deselect all\n"
        "  Enter - Confirm selection\n"
        "  Type - Search agents by name\n"
        "  Up/Down - Navigate the list\n"
        "  Backspace - Clear search character\n"
        "  Ctrl+C - Clear search (twice to quit)\n"
        "  Ctrl+R - Toggle hiding stopped agents"
    )
    instructions = Text(instructions_text)

    header_text = (
        f"    {'NAME'.ljust(name_width)}  {'STATE'.ljust(state_width)}  {'PROVIDER'.ljust(provider_width)}  HOST"
    )
    header_row = AttrMap(Text(("table_header", header_text)), "table_header")

    _refresh_cleanup_list(state)

    header = Pile(
        [
            AttrMap(Text(f"Cleanup: Select Agents to {title_action}", align="center"), "header"),
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

    input_handler = _CleanupInputHandler(state=state)

    with create_urwid_screen_preserving_terminal() as screen:
        loop = MainLoop(
            frame,
            palette=palette,
            unhandled_input=input_handler,
            screen=screen,
        )
        loop.run()

    if state.result is None:
        return []
    return state.result


def _emit_no_agents_found(output_opts: OutputOptions) -> None:
    """Output message when no agents are found."""
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json({"agents": [], "message": "No agents found"})
        case OutputFormat.JSONL:
            emit_event("info", {"message": "No agents found"}, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("No agents found matching the specified filters")
        case _ as unreachable:
            assert_never(unreachable)


def _emit_dry_run_output(
    agents: list[AgentDetails],
    action: CleanupAction,
    output_opts: OutputOptions,
) -> None:
    """Output what would happen in a dry run."""
    match action:
        case CleanupAction.DESTROY:
            action_verb = "Would destroy"
        case CleanupAction.STOP:
            action_verb = "Would stop"
        case _ as unreachable:
            assert_never(unreachable)
    agent_data = [
        {
            "name": str(agent.name),
            "id": str(agent.id),
            "type": agent.type,
            "state": agent.state.value,
            "host_id": str(agent.host.id),
            "provider": str(agent.host.provider_name),
        }
        for agent in agents
    ]

    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json({"action": action.value.lower(), "dry_run": True, "agents": agent_data})
        case OutputFormat.JSONL:
            emit_event("dry_run", {"action": action.value.lower(), "agents": agent_data}, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            write_human_line("\n{} {} agent(s):", action_verb, len(agents))
            for agent in agents:
                write_human_line(
                    "  - {} (type={}, state={}, provider={})",
                    agent.name,
                    agent.type,
                    agent.state.value,
                    agent.host.provider_name,
                )
        case _ as unreachable:
            assert_never(unreachable)


def _emit_result(
    result: CleanupResult,
    output_opts: OutputOptions,
) -> None:
    """Output the final result of the cleanup operation."""
    result_data = {
        "destroyed_agents": [str(n) for n in result.destroyed_agents],
        "stopped_agents": [str(n) for n in result.stopped_agents],
        "errors": result.errors,
        "destroyed_count": len(result.destroyed_agents),
        "stopped_count": len(result.stopped_agents),
        "error_count": len(result.errors),
    }

    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json(result_data)
        case OutputFormat.JSONL:
            emit_event("cleanup_result", result_data, OutputFormat.JSONL)
        case OutputFormat.HUMAN:
            if result.destroyed_agents:
                write_human_line("Successfully destroyed {} agent(s)", len(result.destroyed_agents))
                for name in result.destroyed_agents:
                    write_human_line("  - {}", name)
            if result.stopped_agents:
                write_human_line("Successfully stopped {} agent(s)", len(result.stopped_agents))
                for name in result.stopped_agents:
                    write_human_line("  - {}", name)
            if result.errors:
                logger.warning("{} error(s) occurred:", len(result.errors))
                for error in result.errors:
                    logger.warning("  - {}", error)
            if not result.destroyed_agents and not result.stopped_agents:
                write_human_line("No agents were affected")
        case _ as unreachable:
            assert_never(unreachable)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="cleanup",
    one_line_description="Destroy or stop agents and hosts to free up resources [experimental]",
    synopsis="mng [cleanup|clean] [--destroy|--stop] [--older-than DURATION] [--idle-for DURATION] "
    "[--provider PROVIDER] [--agent-type TYPE] [--tag TAG] [-f|--force|--yes] [--dry-run]",
    description="""When running in a pty, defaults to providing an interactive interface for
reviewing running agents and hosts and selecting which ones to destroy or stop.

When running in a non-interactive setting (or if --yes is provided), will
destroy all selected agents/hosts without prompting.

Convenience filters like --older-than and --idle-for are translated into CEL
expressions internally, so they can be combined with --include and --exclude
for precise control.

For automatic garbage collection of unused resources without interaction,
see `mng gc`.""",
    aliases=("clean",),
    examples=(
        ("Interactive cleanup (default)", "mng cleanup"),
        ("Preview what would be destroyed", "mng cleanup --dry-run --yes"),
        ("Destroy agents older than 7 days", "mng cleanup --older-than 7d --yes"),
        ("Stop idle agents", "mng cleanup --stop --idle-for 1h --yes"),
        ("Destroy Docker agents only", "mng cleanup --provider docker --yes"),
        ("Destroy by agent type", "mng cleanup --agent-type codex --yes"),
    ),
    see_also=(
        ("destroy", "Destroy specific agents by name"),
        ("stop", "Stop specific agents by name"),
        ("gc", "Garbage collect orphaned resources"),
        ("list", "List agents with filtering"),
    ),
).register()

# Add pager-enabled help option to the cleanup command
add_pager_help_option(cleanup)
