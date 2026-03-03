import os
import subprocess
from collections.abc import Hashable
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timezone
from typing import Any

from loguru import logger
from pydantic import ConfigDict
from urwid.display.raw import Screen
from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.event_loop.main_loop import MainLoop
from urwid.widget.attr_map import AttrMap
from urwid.widget.columns import Columns
from urwid.widget.divider import Divider
from urwid.widget.filler import Filler
from urwid.widget.frame import Frame
from urwid.widget.listbox import ListBox
from urwid.widget.listbox import SimpleFocusListWalker
from urwid.widget.pile import Pile
from urwid.widget.text import Text

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mng.config.data_types import MngContext
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import PluginName
from imbue.mng_kanpan.data_types import AgentBoardEntry
from imbue.mng_kanpan.data_types import BoardSection
from imbue.mng_kanpan.data_types import BoardSnapshot
from imbue.mng_kanpan.data_types import CheckStatus
from imbue.mng_kanpan.data_types import CustomCommand
from imbue.mng_kanpan.data_types import KanpanPluginConfig
from imbue.mng_kanpan.data_types import PrState
from imbue.mng_kanpan.fetcher import fetch_board_snapshot
from imbue.mng_kanpan.fetcher import toggle_agent_mute

REFRESH_INTERVAL_SECONDS: int = 600  # 10 minutes

SPINNER_FRAMES: tuple[str, ...] = ("|", "/", "-", "\\")
SPINNER_INTERVAL_SECONDS: float = 0.15
TRANSIENT_MESSAGE_SECONDS: float = 3.0

PALETTE = [
    ("header", "white", "dark blue"),
    ("footer", "white", "dark blue"),
    ("reversed", "standout", ""),
    # Agent states: only RUNNING and WAITING-needing-attention get color
    ("state_running", "light green", ""),
    ("state_running_focus", "light green,standout", ""),
    ("state_attention", "light magenta", ""),
    ("state_attention_focus", "light magenta,standout", ""),
    # Section heading prefixes (the part before the " - ")
    ("section_done", "light magenta", ""),
    ("section_cancelled", "dark gray", ""),
    ("section_in_review", "light cyan", ""),
    ("section_in_progress", "yellow", ""),
    # CI checks (only failing and pending get color; passing is default)
    ("check_failing", "light red", ""),
    ("check_failing_focus", "light red,standout", ""),
    ("check_pending", "yellow", ""),
    ("check_pending_focus", "yellow,standout", ""),
    ("muted", "dark gray", ""),
    ("muted_focus", "dark gray,standout", ""),
    ("section_muted", "dark gray", ""),
    ("error_text", "light red", ""),
    ("notification", "white", "dark magenta"),
]

# Display order: most mature first (like Linear), muted always last
BOARD_SECTION_ORDER: tuple[BoardSection, ...] = (
    BoardSection.PR_MERGED,
    BoardSection.PR_CLOSED,
    BoardSection.PR_BEING_REVIEWED,
    BoardSection.STILL_COOKING,
    BoardSection.MUTED,
)

# Section labels split into colored prefix and plain suffix
_SECTION_PREFIX: dict[BoardSection, str] = {
    BoardSection.PR_MERGED: "Done",
    BoardSection.PR_CLOSED: "Cancelled",
    BoardSection.PR_BEING_REVIEWED: "In review",
    BoardSection.STILL_COOKING: "In progress",
    BoardSection.MUTED: "Muted",
}

_SECTION_SUFFIX: dict[BoardSection, str] = {
    BoardSection.PR_MERGED: "PR merged",
    BoardSection.PR_CLOSED: "PR closed",
    BoardSection.PR_BEING_REVIEWED: "PR pending",
    BoardSection.STILL_COOKING: "no PR yet",
    BoardSection.MUTED: "",
}

_SECTION_ATTR: dict[BoardSection, str] = {
    BoardSection.PR_MERGED: "section_done",
    BoardSection.PR_CLOSED: "section_cancelled",
    BoardSection.PR_BEING_REVIEWED: "section_in_review",
    BoardSection.STILL_COOKING: "section_in_progress",
    BoardSection.MUTED: "section_muted",
}

_CHECK_STATUS_ATTR: dict[CheckStatus, str] = {
    CheckStatus.FAILING: "check_failing",
    CheckStatus.PENDING: "check_pending",
}

# Builtin commands. Users can override these by defining a command with the same key.
# Setting enabled=false on a builtin key disables it.
_BUILTIN_COMMAND_KEY_REFRESH = "r"
_BUILTIN_COMMAND_KEY_PUSH = "p"
_BUILTIN_COMMAND_KEY_DELETE = "d"
_BUILTIN_COMMAND_KEY_MUTE = "m"

_BUILTIN_COMMANDS: dict[str, CustomCommand] = {
    _BUILTIN_COMMAND_KEY_REFRESH: CustomCommand(name="refresh"),
    _BUILTIN_COMMAND_KEY_PUSH: CustomCommand(name="push"),
    _BUILTIN_COMMAND_KEY_DELETE: CustomCommand(name="delete"),
    _BUILTIN_COMMAND_KEY_MUTE: CustomCommand(name="mute"),
}

# All attributes that can appear in agent lines and need focus variants
_AGENT_LINE_ATTRS = ("state_running", "state_attention", "check_failing", "check_pending", "muted")


class _SelectableText(Text):
    """A Text widget that is selectable, allowing it to receive focus.

    Unlike SelectableIcon, this supports full urwid text markup (colored segments).
    """

    _selectable = True

    def keypress(self, size: tuple[()] | tuple[int] | tuple[int, int], key: str) -> str | None:
        """Pass all keys through (no keys are handled by this widget)."""
        return key


class _KanpanState(MutableModel):
    """Mutable state for the pankan TUI."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    mng_ctx: MngContext
    snapshot: BoardSnapshot | None = None
    frame: Any  # urwid Frame widget
    footer_left_text: Any  # urwid Text widget (left side of footer)
    footer_left_attr: Any  # urwid AttrMap wrapping footer_left_text
    footer_right: Any  # urwid Text widget (right side of footer)
    loop: Any = None  # urwid MainLoop, set after construction
    spinner_index: int = 0
    refresh_future: Future[BoardSnapshot] | None = None
    delete_future: Future[subprocess.CompletedProcess[str]] | None = None
    deleting_agent_name: AgentName | None = None
    # Set when awaiting delete confirmation (press d again to confirm, anything else to cancel)
    pending_delete_name: AgentName | None = None
    push_future: Future[subprocess.CompletedProcess[str]] | None = None
    pushing_agent_name: AgentName | None = None
    executor: ThreadPoolExecutor | None = None
    # Maps list walker index -> AgentBoardEntry for selectable agent entries
    index_to_entry: dict[int, AgentBoardEntry] = {}
    list_walker: Any = None  # SimpleFocusListWalker, set during display build
    # Name of the agent that was focused before refresh (for focus persistence)
    focused_agent_name: AgentName | None = None
    # Steady-state footer left text (restored after transient messages)
    steady_footer_text: str = "  Loading..."
    # All commands (builtins merged with user config), keyed by trigger key
    commands: dict[str, CustomCommand] = {}


class _KanpanInputHandler(MutableModel):
    """Callable input handler for the pankan TUI."""

    state: _KanpanState

    def __call__(self, key: str | tuple[str, int, int, int]) -> bool | None:
        """Handle keyboard input. Returns True if handled, None to pass through."""
        if isinstance(key, tuple):
            return None
        # Handle pending delete confirmation
        if self.state.pending_delete_name is not None:
            if key in ("y", "Y"):
                _confirm_delete(self.state)
            else:
                _cancel_delete(self.state)
            return True
        if key in ("q", "Q", "ctrl c"):
            raise ExitMainLoop()
        # Look up command by key (case-insensitive)
        cmd = self.state.commands.get(key.lower())
        if cmd is not None:
            _dispatch_command(self.state, key.lower(), cmd)
            return True
        if key == "up":
            if _is_focus_on_first_selectable(self.state):
                _clear_focus(self.state)
                return True
            return None
        if key in ("down", "page up", "page down", "home", "end"):
            return None
        return True


def _is_focus_on_first_selectable(state: _KanpanState) -> bool:
    """Check if the focus is on the first selectable (agent) entry."""
    if state.list_walker is None:
        return False
    _, focus_index = state.list_walker.get_focus()
    if focus_index is None:
        return False
    # Find the first selectable index
    first_selectable = min(state.index_to_entry.keys()) if state.index_to_entry else None
    return focus_index == first_selectable


def _clear_focus(state: _KanpanState) -> None:
    """Clear agent focus by moving to the first non-selectable widget."""
    state.focused_agent_name = None
    if state.list_walker is not None and len(state.list_walker) > 0:
        # Move focus to position 0 (a section heading, which is non-selectable
        # so it won't highlight, but the ListBox will show the top of the list)
        state.list_walker.set_focus(0)


def _get_focused_entry(state: _KanpanState) -> AgentBoardEntry | None:
    """Get the AgentBoardEntry of the currently focused entry, or None."""
    if state.list_walker is None:
        return None
    _, focus_index = state.list_walker.get_focus()
    if focus_index is None:
        return None
    return state.index_to_entry.get(focus_index)


def _run_destroy(agent_name: str) -> subprocess.CompletedProcess[str]:
    """Run mng destroy in a subprocess. Called from a background thread."""
    return subprocess.run(
        ["mng", "destroy", agent_name, "--force"],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _is_safe_to_delete(entry: AgentBoardEntry) -> bool:
    """Check if an agent can be deleted without confirmation.

    Agents with a merged PR are safe to delete. All others require confirmation.
    """
    return entry.pr is not None and entry.pr.state == PrState.MERGED


def _delete_focused_agent(state: _KanpanState) -> None:
    """Delete the focused agent, with confirmation if PR is not merged."""
    if state.delete_future is not None:
        return  # Already deleting
    entry = _get_focused_entry(state)
    if entry is None:
        return

    if _is_safe_to_delete(entry):
        _execute_delete(state, entry.name)
    else:
        state.pending_delete_name = entry.name
        state.footer_left_text.set_text(
            f"  Delete {entry.name}? PR not merged. Press y to confirm, any other key to cancel"
        )
        state.footer_left_attr.set_attr_map({None: "notification"})


def _confirm_delete(state: _KanpanState) -> None:
    """Confirm a pending delete."""
    agent_name = state.pending_delete_name
    state.pending_delete_name = None
    state.footer_left_attr.set_attr_map({None: "footer"})
    if agent_name is not None:
        _execute_delete(state, agent_name)


def _cancel_delete(state: _KanpanState) -> None:
    """Cancel a pending delete."""
    state.pending_delete_name = None
    _restore_footer(state)


def _execute_delete(state: _KanpanState, agent_name: AgentName) -> None:
    """Execute the actual deletion of an agent."""
    if state.executor is None:
        state.executor = ThreadPoolExecutor(max_workers=1)

    state.deleting_agent_name = agent_name
    state.footer_left_text.set_text(f"  Deleting {agent_name}...")
    state.delete_future = state.executor.submit(_run_destroy, str(agent_name))

    if state.loop is not None:
        state.loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_delete_poll, state)


def _on_delete_poll(loop: MainLoop, state: _KanpanState) -> None:
    """Poll for delete completion."""
    if state.delete_future is None:
        return

    if state.delete_future.done():
        _finish_delete(loop, state)
        return

    # Show spinner while deleting
    frame_char = SPINNER_FRAMES[state.spinner_index % len(SPINNER_FRAMES)]
    state.footer_left_text.set_text(f"  Deleting {state.deleting_agent_name} {frame_char}")
    state.spinner_index += 1
    loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_delete_poll, state)


def _finish_delete(loop: MainLoop, state: _KanpanState) -> None:
    """Complete a background deletion."""
    if state.delete_future is None:
        return

    agent_name = state.deleting_agent_name
    try:
        result = state.delete_future.result()
        if result.returncode == 0:
            _show_transient_message(state, f"  Deleted {agent_name}")
        else:
            stderr = result.stderr.strip()
            _show_transient_message(state, f"  Failed to delete {agent_name}: {stderr}")
    except Exception as e:
        _show_transient_message(state, f"  Failed to delete {agent_name}: {e}")
    finally:
        state.delete_future = None
        state.deleting_agent_name = None

    # Trigger a refresh to update the board
    if state.refresh_future is None:
        _start_refresh(loop, state)


def _run_git_push(work_dir: str) -> subprocess.CompletedProcess[str]:
    """Run git push in an agent's work_dir. Called from a background thread."""
    return subprocess.run(
        ["git", "push", "-u", "origin", "HEAD"],
        capture_output=True,
        text=True,
        cwd=work_dir,
        timeout=60,
    )


def _push_focused_agent(state: _KanpanState) -> None:
    """Start async push of the currently focused agent's branch."""
    if state.push_future is not None:
        return  # Already pushing
    entry = _get_focused_entry(state)
    if entry is None:
        return
    if entry.work_dir is None:
        _show_transient_message(state, f"  Cannot push: {entry.name} has no local work_dir")
        return
    if state.executor is None:
        state.executor = ThreadPoolExecutor(max_workers=1)

    state.pushing_agent_name = entry.name
    state.footer_left_text.set_text(f"  Pushing {entry.name}...")
    state.push_future = state.executor.submit(_run_git_push, str(entry.work_dir))

    if state.loop is not None:
        state.loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_push_poll, state)


def _on_push_poll(loop: MainLoop, state: _KanpanState) -> None:
    """Poll for push completion."""
    if state.push_future is None:
        return

    if state.push_future.done():
        _finish_push(loop, state)
        return

    frame_char = SPINNER_FRAMES[state.spinner_index % len(SPINNER_FRAMES)]
    state.footer_left_text.set_text(f"  Pushing {state.pushing_agent_name} {frame_char}")
    state.spinner_index += 1
    loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_push_poll, state)


def _finish_push(loop: MainLoop, state: _KanpanState) -> None:
    """Complete a background push."""
    if state.push_future is None:
        return

    agent_name = state.pushing_agent_name
    try:
        result = state.push_future.result()
        if result.returncode == 0:
            _show_transient_message(state, f"  Pushed {agent_name}")
        else:
            stderr = result.stderr.strip()
            _show_transient_message(state, f"  Failed to push {agent_name}: {stderr}")
    except Exception as e:
        _show_transient_message(state, f"  Failed to push {agent_name}: {e}")
    finally:
        state.push_future = None
        state.pushing_agent_name = None

    # Trigger a refresh to update the board
    if state.refresh_future is None:
        _start_refresh(loop, state)


def _update_snapshot_mute(state: _KanpanState, agent_name: AgentName, is_muted: bool) -> None:
    """Update the snapshot in-place by toggling is_muted on the named agent."""
    if state.snapshot is None:
        return
    new_entries = tuple(
        entry.model_copy(update={"is_muted": is_muted}) if entry.name == agent_name else entry
        for entry in state.snapshot.entries
    )
    state.snapshot = BoardSnapshot(
        entries=new_entries,
        errors=state.snapshot.errors,
        fetch_time_seconds=state.snapshot.fetch_time_seconds,
    )


def _mute_focused_agent(state: _KanpanState) -> None:
    """Toggle mute on the currently focused agent.

    Optimistically updates the UI immediately, then persists in the background.
    If the persist fails, reverts the UI change.
    """
    entry = _get_focused_entry(state)
    if entry is None:
        return
    if state.executor is None:
        state.executor = ThreadPoolExecutor(max_workers=1)

    agent_name = entry.name
    new_muted = not entry.is_muted

    # Optimistic UI update
    _update_snapshot_mute(state, agent_name, new_muted)
    _refresh_display(state)
    action = "Muted" if new_muted else "Unmuted"
    _show_transient_message(state, f"  {action} {agent_name}")

    # Persist in background
    def _do_mute() -> bool:
        return toggle_agent_mute(state.mng_ctx, agent_name)

    future = state.executor.submit(_do_mute)
    if state.loop is not None:
        state.loop.set_alarm_in(
            SPINNER_INTERVAL_SECONDS, _on_mute_persist_poll, (state, future, agent_name, new_muted)
        )


def _on_mute_persist_poll(loop: MainLoop, data: tuple[_KanpanState, Future[bool], AgentName, bool]) -> None:
    """Poll for mute persist completion. Revert UI on failure."""
    state, future, agent_name, expected_muted = data
    if future.done():
        try:
            future.result()
        except Exception as e:
            # Revert the optimistic update
            _update_snapshot_mute(state, agent_name, not expected_muted)
            _refresh_display(state)
            _show_transient_message(state, f"  Failed to persist mute for {agent_name}: {e}")
    else:
        loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_mute_persist_poll, data)


def _dispatch_command(state: _KanpanState, key: str, cmd: CustomCommand) -> None:
    """Dispatch a command by key. Routes to builtins or runs shell commands."""
    if key == _BUILTIN_COMMAND_KEY_REFRESH and not cmd.command:
        if state.refresh_future is None and state.loop is not None:
            _start_refresh(state.loop, state)
        return
    if key == _BUILTIN_COMMAND_KEY_PUSH and not cmd.command:
        _push_focused_agent(state)
        return
    if key == _BUILTIN_COMMAND_KEY_DELETE and not cmd.command:
        _delete_focused_agent(state)
        return
    if key == _BUILTIN_COMMAND_KEY_MUTE and not cmd.command:
        _mute_focused_agent(state)
        return
    # User-defined shell command
    if cmd.command:
        _run_shell_command(state, cmd)


def _run_shell_command(state: _KanpanState, cmd: CustomCommand) -> None:
    """Run a user-defined custom command on the focused agent."""
    entry = _get_focused_entry(state)
    if entry is None:
        return
    if state.executor is None:
        state.executor = ThreadPoolExecutor(max_workers=1)

    agent_name = entry.name
    state.footer_left_text.set_text(f"  Running {cmd.name} on {agent_name}...")

    def _do_run() -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "MNG_AGENT_NAME": str(agent_name)}
        return subprocess.run(
            cmd.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    future = state.executor.submit(_do_run)
    if state.loop is not None:
        state.loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_custom_command_poll, (state, future, cmd, agent_name))


def _on_custom_command_poll(
    loop: MainLoop, data: tuple[_KanpanState, Future[subprocess.CompletedProcess[str]], CustomCommand, AgentName]
) -> None:
    """Poll for custom command completion."""
    state, future, cmd, agent_name = data
    if future.done():
        try:
            result = future.result()
            if result.returncode == 0:
                _show_transient_message(state, f"  {cmd.name} completed for {agent_name}")
            else:
                stderr = result.stderr.strip()
                _show_transient_message(state, f"  {cmd.name} failed for {agent_name}: {stderr}")
        except Exception as e:
            _show_transient_message(state, f"  {cmd.name} failed for {agent_name}: {e}")
        if cmd.refresh_afterwards and state.refresh_future is None:
            _start_refresh(loop, state)
    else:
        frame_char = SPINNER_FRAMES[state.spinner_index % len(SPINNER_FRAMES)]
        state.footer_left_text.set_text(f"  Running {cmd.name} on {agent_name} {frame_char}")
        state.spinner_index += 1
        loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_custom_command_poll, data)


def _show_transient_message(state: _KanpanState, message: str) -> None:
    """Show a transient notification in the footer that auto-reverts after a few seconds."""
    state.footer_left_text.set_text(message)
    state.footer_left_attr.set_attr_map({None: "notification"})
    if state.loop is not None:
        state.loop.set_alarm_in(TRANSIENT_MESSAGE_SECONDS, _on_restore_footer, state)


def _restore_footer(state: _KanpanState) -> None:
    """Restore the steady-state footer styling and text."""
    state.footer_left_text.set_text(state.steady_footer_text)
    state.footer_left_attr.set_attr_map({None: "footer"})


def _on_restore_footer(loop: MainLoop, state: _KanpanState) -> None:
    """Alarm callback to restore the steady-state footer."""
    _restore_footer(state)


def _start_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Start a background refresh and begin the spinner animation."""
    if state.executor is None:
        state.executor = ThreadPoolExecutor(max_workers=1)
    state.footer_left_attr.set_attr_map({None: "footer"})
    state.spinner_index = 0
    state.refresh_future = state.executor.submit(fetch_board_snapshot, state.mng_ctx)
    _schedule_spinner_tick(loop, state)


def _schedule_spinner_tick(loop: MainLoop, state: _KanpanState) -> None:
    """Schedule the next spinner tick."""
    loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_spinner_tick, state)


def _on_spinner_tick(loop: MainLoop, state: _KanpanState) -> None:
    """Alarm callback: update spinner, check if fetch is done."""
    if state.refresh_future is None:
        return

    if state.refresh_future.done():
        _finish_refresh(loop, state)
        return

    # Animate spinner
    frame_char = SPINNER_FRAMES[state.spinner_index % len(SPINNER_FRAMES)]
    state.footer_left_text.set_text(f"  Refreshing {frame_char}")
    state.spinner_index += 1
    _schedule_spinner_tick(loop, state)


def _finish_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Complete a background refresh: update snapshot and display."""
    if state.refresh_future is None:
        return

    try:
        state.snapshot = state.refresh_future.result()
    except Exception as e:
        logger.debug("Refresh failed: {}", e)
        if state.snapshot is not None:
            state.snapshot = BoardSnapshot(
                entries=state.snapshot.entries,
                errors=(*state.snapshot.errors, f"Refresh failed: {e}"),
                fetch_time_seconds=state.snapshot.fetch_time_seconds,
            )
    finally:
        state.refresh_future = None

    _refresh_display(state)

    now = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
    if state.snapshot is not None:
        elapsed = f"{state.snapshot.fetch_time_seconds:.1f}s"
        state.steady_footer_text = f"  Last refresh: {now} (took {elapsed})  r: refresh"
    else:
        state.steady_footer_text = f"  Last refresh: {now}  r: refresh"
    state.footer_left_text.set_text(state.steady_footer_text)

    _schedule_next_refresh(loop, state)


def _classify_entry(entry: AgentBoardEntry) -> BoardSection:
    """Determine which board section an agent belongs to based on its PR state.

    Muted agents are always placed in the MUTED section regardless of PR state.
    """
    if entry.is_muted:
        return BoardSection.MUTED
    if entry.pr is None:
        return BoardSection.STILL_COOKING
    if entry.pr.state == PrState.MERGED:
        return BoardSection.PR_MERGED
    if entry.pr.state == PrState.CLOSED:
        return BoardSection.PR_CLOSED
    return BoardSection.PR_BEING_REVIEWED


def _get_state_attr(entry: AgentBoardEntry, section: BoardSection) -> str:
    """Determine the color attribute for an agent's lifecycle state.

    Magenta is used for WAITING agents in the "still cooking" section
    (they need the user to respond). RUNNING gets green. Everything
    else is default (no color).
    """
    if entry.state == AgentLifecycleState.RUNNING:
        return "state_running"
    if entry.state == AgentLifecycleState.WAITING and section == BoardSection.STILL_COOKING:
        return "state_attention"
    return ""


def _format_check_markup(entry: AgentBoardEntry) -> list[str | tuple[Hashable, str]]:
    """Build urwid text markup for CI check status.

    Only failing and pending checks get color. Passing checks are shown
    in default color. Unknown checks are not shown at all.
    """
    if entry.pr is None or entry.pr.check_status == CheckStatus.UNKNOWN:
        return []
    check_attr = _CHECK_STATUS_ATTR.get(entry.pr.check_status)
    if check_attr is not None:
        return ["  CI ", (check_attr, entry.pr.check_status.lower())]
    # PASSING: show in default color
    return [f"  CI {entry.pr.check_status.lower()}"]


def _format_push_status(entry: AgentBoardEntry) -> str:
    """Build text for push status indicator."""
    if entry.commits_ahead is None:
        return "  [not pushed]"
    if entry.commits_ahead == 0:
        return "  [up to date]"
    return f"  [{entry.commits_ahead} unpushed]"


def _format_agent_line(entry: AgentBoardEntry, section: BoardSection) -> list[str | tuple[Hashable, str]]:
    """Build urwid text markup for a single agent line.

    Shows: name, agent state, push status, PR info or create-PR link.
    Muted agents show the same information but rendered entirely in gray.
    """
    state_attr = _get_state_attr(entry, section)
    state_text = str(entry.state)
    parts: list[str | tuple[Hashable, str]] = [
        f"  {entry.name:<24}",
    ]
    if state_attr:
        parts.append((state_attr, state_text))
    else:
        parts.append(state_text)

    # Push status for local agents
    if entry.work_dir is not None:
        parts.append(_format_push_status(entry))

    if entry.pr is not None:
        parts.append(f"  PR #{entry.pr.number}")
        parts.extend(_format_check_markup(entry))
        parts.append(f"  {entry.pr.url}")
    elif entry.create_pr_url is not None:
        parts.append(f"  create PR: {entry.create_pr_url}")

    # For muted agents, flatten everything to gray
    if section == BoardSection.MUTED:
        plain = "".join(seg if isinstance(seg, str) else seg[1] for seg in parts)
        return [("muted", plain)]

    return parts


def _format_section_heading(section: BoardSection, count: int) -> list[str | tuple[Hashable, str]]:
    """Build urwid text markup for a section heading.

    Only the prefix (e.g. "Done") is colored; the rest is default.
    """
    prefix = _SECTION_PREFIX[section]
    suffix = _SECTION_SUFFIX[section]
    attr = _SECTION_ATTR[section]
    if suffix:
        return [(attr, prefix), f" - {suffix} ({count})"]
    return [(attr, prefix), f" ({count})"]


def _build_board_widgets(state: _KanpanState) -> SimpleFocusListWalker[AttrMap | Text | Divider]:
    """Build the urwid widget list from a BoardSnapshot, grouped by PR state.

    Returns a SimpleFocusListWalker and populates state.index_to_entry with the
    mapping from list walker index to agent name for selectable entries.
    """
    snapshot = state.snapshot
    state.index_to_entry = {}

    walker: SimpleFocusListWalker[AttrMap | Text | Divider] = SimpleFocusListWalker([])

    if snapshot is None:
        walker.append(Text("Loading..."))
        return walker

    # Classify entries into sections
    by_section: dict[BoardSection, list[AgentBoardEntry]] = {}
    for entry in snapshot.entries:
        section = _classify_entry(entry)
        by_section.setdefault(section, []).append(entry)

    has_content = False

    for section in BOARD_SECTION_ORDER:
        entries = by_section.get(section)
        if not entries:
            continue

        if has_content:
            walker.append(Divider())

        walker.append(Text(_format_section_heading(section, len(entries))))
        has_content = True

        for entry in entries:
            markup = _format_agent_line(entry, section)
            item = _SelectableText(markup)
            idx = len(walker)
            focus_map: dict[str | None, str] = {None: "reversed"}
            for attr in _AGENT_LINE_ATTRS:
                focus_map[attr] = f"{attr}_focus"
            walker.append(AttrMap(item, None, focus_map=focus_map))
            state.index_to_entry[idx] = entry

    if not has_content:
        walker.append(Text("No agents found."))

    # Show errors if any
    if snapshot.errors:
        walker.append(Divider())
        walker.append(Text(("error_text", "Errors:")))
        for error in snapshot.errors:
            walker.append(Text(("error_text", f"  {error}")))

    return walker


def _refresh_display(state: _KanpanState) -> None:
    """Rebuild the body display from the current snapshot.

    Preserves focus on the previously selected agent if it still exists.
    """
    # Save the currently focused agent name before rebuilding
    focused_entry = _get_focused_entry(state)
    if focused_entry is not None:
        state.focused_agent_name = focused_entry.name

    walker = _build_board_widgets(state)
    state.list_walker = walker
    state.frame.body = ListBox(walker)

    # Restore focus to the previously focused agent
    if state.focused_agent_name is not None:
        for idx, entry in state.index_to_entry.items():
            if entry.name == state.focused_agent_name:
                walker.set_focus(idx)
                return


def _schedule_next_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Schedule the next auto-refresh alarm."""
    loop.set_alarm_in(REFRESH_INTERVAL_SECONDS, _on_auto_refresh_alarm, state)


def _on_auto_refresh_alarm(loop: MainLoop, state: _KanpanState) -> None:
    """Alarm callback for periodic auto-refresh."""
    if state.refresh_future is None:
        _start_refresh(loop, state)


def _load_user_commands(mng_ctx: MngContext) -> dict[str, CustomCommand]:
    """Load user-defined commands from plugin config."""
    plugin_name = PluginName("kanpan")
    if plugin_name not in mng_ctx.config.plugins:
        return {}
    config = mng_ctx.config.plugins[plugin_name]
    if not isinstance(config, KanpanPluginConfig):
        return {}
    # Config loader uses model_construct() which bypasses validation,
    # so nested dicts may not be parsed into CustomCommand objects.
    result: dict[str, CustomCommand] = {}
    for key, value in config.commands.items():
        if isinstance(value, CustomCommand):
            result[key] = value
        elif isinstance(value, dict):
            result[key] = CustomCommand(**value)
    return result


def _build_command_map(mng_ctx: MngContext) -> dict[str, CustomCommand]:
    """Build the unified command map: builtins merged with user config.

    User commands override builtins when they share the same key.
    Commands with enabled=False are filtered out.
    """
    commands = dict(_BUILTIN_COMMANDS)
    user_commands = _load_user_commands(mng_ctx)
    commands.update(user_commands)
    return {key: cmd for key, cmd in commands.items() if cmd.enabled}


def run_kanpan(mng_ctx: MngContext) -> None:
    """Run the kanpan TUI board."""
    commands = _build_command_map(mng_ctx)

    # Build footer keybindings (q: quit is always present, not in the command map)
    keybinding_parts = [f"{key}: {cmd.name}" for key, cmd in commands.items()]
    keybinding_parts.append("q: quit")
    keybindings = "  ".join(keybinding_parts) + "  "

    footer_left_text = Text("  Loading...")
    footer_left_attr = AttrMap(footer_left_text, "footer")
    footer_right = Text(keybindings, align="right")
    pack: int = len(keybindings)
    footer_columns = Columns([footer_left_attr, (pack, AttrMap(footer_right, "footer"))])
    footer = Pile([Divider(), footer_columns])

    header = Pile(
        [
            AttrMap(Text("Kanpan - all-seeing agent tracker - 看 πᾶν", align="center"), "header"),
            Divider(),
        ]
    )

    initial_body = Filler(Pile([Text("Loading...")]), valign="top")
    frame = Frame(body=initial_body, header=header, footer=footer)

    state = _KanpanState(
        mng_ctx=mng_ctx,
        frame=frame,
        footer_left_text=footer_left_text,
        footer_left_attr=footer_left_attr,
        footer_right=footer_right,
        commands=commands,
    )

    input_handler = _KanpanInputHandler(state=state)

    screen = Screen()
    screen.tty_signal_keys(intr="undefined")

    loop = MainLoop(frame, palette=PALETTE, unhandled_input=input_handler, screen=screen)
    state.loop = loop

    # Initial data load with spinner
    _start_refresh(loop, state)

    logger.disable("imbue")
    try:
        loop.run()
    finally:
        logger.enable("imbue")
        if state.executor is not None:
            state.executor.shutdown(wait=False)
