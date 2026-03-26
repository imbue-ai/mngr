import os
import subprocess
import time
from collections.abc import Callable
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

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mng.config.data_types import MngContext
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng_kanpan.data_types import AgentBoardEntry
from imbue.mng_kanpan.data_types import BoardSection
from imbue.mng_kanpan.data_types import BoardSnapshot
from imbue.mng_kanpan.data_types import CheckStatus
from imbue.mng_kanpan.data_types import CustomColumnConfig
from imbue.mng_kanpan.data_types import CustomCommand
from imbue.mng_kanpan.data_types import KanpanPluginConfig
from imbue.mng_kanpan.data_types import PrState
from imbue.mng_kanpan.data_types import RefreshHook
from imbue.mng_kanpan.fetcher import fetch_agent_snapshot
from imbue.mng_kanpan.fetcher import fetch_board_snapshot
from imbue.mng_kanpan.fetcher import toggle_agent_mute

DEFAULT_REFRESH_INTERVAL_SECONDS: float = 600.0

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
_BUILTIN_COMMAND_KEY_UNMARK = "u"
_BUILTIN_COMMAND_KEY_EXECUTE = "x"

_BUILTIN_COMMANDS: dict[str, CustomCommand] = {
    _BUILTIN_COMMAND_KEY_REFRESH: CustomCommand(name="refresh"),
    _BUILTIN_COMMAND_KEY_PUSH: CustomCommand(name="mark push", markable="yellow"),
    _BUILTIN_COMMAND_KEY_DELETE: CustomCommand(name="mark delete", markable="light red"),
    _BUILTIN_COMMAND_KEY_MUTE: CustomCommand(name="mute"),
    _BUILTIN_COMMAND_KEY_UNMARK: CustomCommand(name="unmark"),
    _BUILTIN_COMMAND_KEY_EXECUTE: CustomCommand(name="execute"),
}

_DEFAULT_MARK_COLOR = "light cyan"

# All attributes that can appear in agent lines and need focus variants
_AGENT_LINE_ATTRS = (
    "state_running",
    "state_attention",
    "check_failing",
    "check_pending",
    "muted",
)

# Column layout configuration
_COL_DIVIDER_CHARS = 2


class _SelectableRow(Columns):
    """A Columns widget that is selectable, allowing it to receive focus.

    Columns.selectable() checks children rather than _selectable, so we
    must override it explicitly to make the widget focusable in a ListBox.
    """

    def selectable(self) -> bool:
        return True

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
    executor: ThreadPoolExecutor | None = None
    # Dired-style marks: agents flagged for batch operations, keyed by command key
    marks: dict[AgentName, str] = {}
    # Active batch execution state
    executing: bool = False
    execute_status: str = ""
    # Maps list walker index -> AgentBoardEntry for selectable agent entries
    index_to_entry: dict[int, AgentBoardEntry] = {}
    list_walker: Any = None  # SimpleFocusListWalker, set during display build
    # Name of the agent that was focused before refresh (for focus persistence)
    focused_agent_name: AgentName | None = None
    # Steady-state footer left text (restored after transient messages)
    steady_footer_text: str = "  Loading..."
    # All commands (builtins merged with user config), keyed by trigger key
    commands: dict[str, CustomCommand] = {}
    # Monotonic timestamp of the last completed refresh (for cooldown logic)
    last_refresh_time: float = 0.0
    # Whether the current in-flight refresh is local-only (no GitHub API)
    refresh_is_local_only: bool = False
    # Handle for the pending deferred refresh alarm (None if no alarm is pending)
    deferred_refresh_alarm: Any = None
    # Monotonic time the deferred refresh is scheduled to fire
    deferred_refresh_fire_at: float = 0.0
    # Cooldown durations (loaded from plugin config)
    refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS
    retry_cooldown_seconds: float = 60.0
    # Palette attr names for mark indicators (e.g. "mark_d", "mark_p")
    mark_attr_names: tuple[str, ...] = ()
    # Column definitions (builtins + any custom columns from config)
    column_defs: list["_ColumnDef"] = []  # populated from _BOARD_COLUMN_DEFS at startup
    # Palette attr names for custom column colors
    col_attr_names: tuple[str, ...] = ()
    # Refresh hooks loaded from plugin config
    on_before_refresh: list[RefreshHook] = []
    on_after_refresh: list[RefreshHook] = []
    # CEL filter expressions passed from CLI
    include_filters: tuple[str, ...] = ()
    exclude_filters: tuple[str, ...] = ()


class _KanpanInputHandler(MutableModel):
    """Callable input handler for the pankan TUI."""

    state: _KanpanState

    def __call__(self, key: str | tuple[str, int, int, int]) -> bool | None:
        """Handle keyboard input. Returns True if handled, None to pass through."""
        if isinstance(key, tuple):
            return None
        if key in ("q", "ctrl c"):
            raise ExitMainLoop()
        if key == "U":
            _unmark_all(self.state)
            return True
        cmd = self.state.commands.get(key)
        if cmd is not None:
            _dispatch_command(self.state, key, cmd)
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


def _run_destroy(agent_name: str) -> subprocess.CompletedProcess[str]:  # pragma: no cover
    """Run mng destroy in a subprocess. Called from a background thread."""
    return subprocess.run(
        ["mng", "destroy", agent_name, "--force"],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _run_git_push(work_dir: str) -> subprocess.CompletedProcess[str]:  # pragma: no cover
    """Run git push in an agent's work_dir. Called from a background thread."""
    return subprocess.run(
        ["git", "push", "-u", "origin", "HEAD"],
        capture_output=True,
        text=True,
        cwd=work_dir,
        timeout=60,
    )


def _update_row_mark(state: _KanpanState, walker_idx: int, mark_key: str | None) -> None:
    """Update the mark indicator on a single row without rebuilding the display."""
    if state.list_walker is None:
        return
    entry = state.index_to_entry.get(walker_idx)
    if entry is None:
        return
    section = _classify_entry(entry)
    name_markup: str | tuple[Hashable, str] | list[str | tuple[Hashable, str]] = _get_name_cell_markup(entry, mark_key)
    if section == BoardSection.MUTED:
        name_markup = _flatten_markup_to_muted(name_markup)
    attr_map_widget = state.list_walker[walker_idx]
    row: _SelectableRow = attr_map_widget.original_widget
    name_text: Text = row.contents[0][0]
    name_text.set_text(name_markup)


def _toggle_mark(state: _KanpanState, key: str) -> None:
    """Toggle a dired-style mark on the focused agent."""
    if state.list_walker is None:
        return
    _, focus_idx = state.list_walker.get_focus()
    if focus_idx is None:
        return
    entry = state.index_to_entry.get(focus_idx)
    if entry is None:
        return

    if key == _BUILTIN_COMMAND_KEY_PUSH and entry.work_dir is None:
        _show_transient_message(state, f"  Cannot push: {entry.name} has no local work_dir")
        return

    existing = state.marks.get(entry.name)
    if existing == key:
        del state.marks[entry.name]
        new_mark = None
    else:
        state.marks[entry.name] = key
        new_mark = key

    _update_row_mark(state, focus_idx, new_mark)
    _update_mark_count_footer(state)


def _unmark_focused(state: _KanpanState) -> None:
    """Remove any mark from the focused agent."""
    if state.list_walker is None:
        return
    _, focus_idx = state.list_walker.get_focus()
    if focus_idx is None:
        return
    entry = state.index_to_entry.get(focus_idx)
    if entry is None:
        return
    if entry.name in state.marks:
        del state.marks[entry.name]
        _update_row_mark(state, focus_idx, None)
        _update_mark_count_footer(state)


def _unmark_all(state: _KanpanState) -> None:
    """Remove all marks."""
    if not state.marks:
        return
    # Update each marked row's display before clearing
    marked_names = set(state.marks.keys())
    state.marks.clear()
    for idx, entry in state.index_to_entry.items():
        if entry.name in marked_names:
            _update_row_mark(state, idx, None)
    _update_mark_count_footer(state)


def _update_mark_count_footer(state: _KanpanState) -> None:
    """Update the footer to show the count of marked agents."""
    if not state.marks:
        _restore_footer(state)
        return
    counts: dict[str, int] = {}
    for mark_key in state.marks.values():
        counts[mark_key] = counts.get(mark_key, 0) + 1
    parts = []
    for mark_key, count in sorted(counts.items()):
        cmd = state.commands.get(mark_key)
        label = cmd.name if cmd else mark_key
        parts.append(f"{count} {label}")
    state.footer_left_text.set_text(f"  Marked: {', '.join(parts)}  (x to execute, U to unmark all)")
    state.footer_left_attr.set_attr_map({None: "footer"})


def _execute_marks(state: _KanpanState) -> None:
    """Execute all pending marks immediately."""
    if not state.marks or state.executing:
        return
    _start_batch_execution(state)


class _BatchWorkItem(FrozenModel):
    name: AgentName
    key: str
    cmd: CustomCommand
    entry: AgentBoardEntry | None


def _run_shell_command_sync(command: str, agent_name: str) -> subprocess.CompletedProcess[str]:
    """Run a shell command with MNG_AGENT_NAME set. Called from a background thread."""
    env = {**os.environ, "MNG_AGENT_NAME": agent_name}
    return subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _start_batch_execution(state: _KanpanState) -> None:
    """Begin executing all marked operations sequentially."""
    if state.executor is None:
        state.executor = ThreadPoolExecutor(max_workers=1)

    state.executing = True
    state.spinner_index = 0

    entries_by_name: dict[AgentName, AgentBoardEntry] = {}
    if state.snapshot is not None:
        entries_by_name = {e.name: e for e in state.snapshot.entries}

    work: list[_BatchWorkItem] = []
    for name, mark_key in state.marks.items():
        cmd = state.commands.get(mark_key)
        if cmd is not None:
            work.append(_BatchWorkItem(name=name, key=mark_key, cmd=cmd, entry=entries_by_name.get(name)))

    _execute_next_in_batch(state, work, [], 0)


def _submit_batch_item(
    executor: ThreadPoolExecutor, item: _BatchWorkItem
) -> Future[subprocess.CompletedProcess[str]] | None:
    """Submit a single batch work item to the executor. Returns None if the item can't be executed."""
    if item.key == _BUILTIN_COMMAND_KEY_DELETE:
        return executor.submit(_run_destroy, str(item.name))
    if item.key == _BUILTIN_COMMAND_KEY_PUSH:
        if item.entry is None or item.entry.work_dir is None:
            return None
        return executor.submit(_run_git_push, str(item.entry.work_dir))
    if item.cmd.command:
        return executor.submit(_run_shell_command_sync, item.cmd.command, str(item.name))
    return None


def _execute_next_in_batch(
    state: _KanpanState,
    work: list[_BatchWorkItem],
    results: list[str],
    index: int,
) -> None:
    """Execute the next item in the batch work queue."""
    if index >= len(work):
        _finish_batch_execution(state, results)
        return

    item = work[index]
    state.execute_status = f"  [{index + 1}/{len(work)}] "

    assert state.executor is not None
    future = _submit_batch_item(state.executor, item)
    if future is None:
        results.append(f"{item.cmd.name} {item.name}: skipped (not executable)")
        _execute_next_in_batch(state, work, results, index + 1)
        return

    state.footer_left_text.set_text(f"{state.execute_status}{item.cmd.name} {item.name}...")

    if state.loop is not None:
        state.loop.set_alarm_in(
            SPINNER_INTERVAL_SECONDS,
            _on_batch_item_poll,
            (state, future, work, results, index, item),
        )


def _on_batch_item_poll(
    loop: MainLoop,
    data: tuple[
        _KanpanState,
        Future[subprocess.CompletedProcess[str]],
        list[_BatchWorkItem],
        list[str],
        int,
        _BatchWorkItem,
    ],
) -> None:
    """Poll for completion of a single batch item."""
    state, future, work, results, index, item = data

    if future.done():
        try:
            result = future.result()
            if result.returncode == 0:
                results.append(f"{item.cmd.name} {item.name}: ok")
                state.marks.pop(item.name, None)
            else:
                stderr = result.stderr.strip()
                results.append(f"{item.cmd.name} {item.name}: failed ({stderr})")
        except Exception as e:
            results.append(f"{item.cmd.name} {item.name}: failed ({e})")

        _execute_next_in_batch(state, work, results, index + 1)
        return

    frame_char = SPINNER_FRAMES[state.spinner_index % len(SPINNER_FRAMES)]
    state.footer_left_text.set_text(f"{state.execute_status}{item.cmd.name} {item.name} {frame_char}")
    state.spinner_index += 1
    loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_batch_item_poll, data)


def _finish_batch_execution(state: _KanpanState, results: list[str]) -> None:
    """Complete batch execution and show summary."""
    state.executing = False
    state.execute_status = ""

    ok_count = sum(1 for r in results if r.endswith(": ok"))
    fail_count = len(results) - ok_count

    if fail_count == 0:
        _show_transient_message(state, f"  Executed {ok_count} operation(s) successfully")
    else:
        _show_transient_message(state, f"  Executed: {ok_count} ok, {fail_count} failed")

    _refresh_display(state)

    # Local-only refresh to immediately show updated state (no cooldown needed)
    if state.loop is not None:
        _start_local_refresh(state.loop, state)


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
        prs_loaded=state.snapshot.prs_loaded,
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
    """Dispatch a command by key. Routes to builtins, markable commands, or immediate shell commands."""
    if key == _BUILTIN_COMMAND_KEY_REFRESH and not cmd.command:
        if state.loop is not None and state.refresh_future is None:
            _start_refresh(state.loop, state)
        return
    if key == _BUILTIN_COMMAND_KEY_MUTE and not cmd.command:
        _mute_focused_agent(state)
        return
    if key == _BUILTIN_COMMAND_KEY_UNMARK and not cmd.command:
        _unmark_focused(state)
        return
    if key == _BUILTIN_COMMAND_KEY_EXECUTE and not cmd.command:
        _execute_marks(state)
        return
    if cmd.markable:
        _toggle_mark(state, key)
        return
    # Immediate shell command
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
        if cmd.refresh_afterwards:
            _start_local_refresh(loop, state)
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


def _request_refresh(loop: MainLoop, state: _KanpanState, cooldown_seconds: float) -> None:
    """Request a refresh, subject to a cooldown period.

    If enough time has passed since the last refresh, starts immediately.
    Otherwise, schedules a deferred refresh for when the cooldown expires.
    If a deferred refresh is already pending but the new request would fire
    sooner (e.g. manual refresh with a shorter cooldown), the old alarm is
    replaced.
    """
    if state.refresh_future is not None:
        return
    elapsed = time.monotonic() - state.last_refresh_time
    remaining = cooldown_seconds - elapsed
    if remaining <= 0:
        _cancel_deferred_refresh(loop, state)
        _start_refresh(loop, state)
        return
    fire_at = time.monotonic() + remaining
    if state.deferred_refresh_alarm is not None:
        if state.deferred_refresh_fire_at <= fire_at:
            return
        _cancel_deferred_refresh(loop, state)
    state.deferred_refresh_fire_at = fire_at
    state.deferred_refresh_alarm = loop.set_alarm_in(remaining, _on_deferred_refresh, state)


def _cancel_deferred_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Cancel any pending deferred refresh alarm."""
    if state.deferred_refresh_alarm is not None:
        loop.remove_alarm(state.deferred_refresh_alarm)
        state.deferred_refresh_alarm = None


def _on_deferred_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Alarm callback for a deferred (cooldown-delayed) refresh."""
    state.deferred_refresh_alarm = None
    if state.refresh_future is None:
        _start_refresh(loop, state)


def _start_local_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Start a local-only background refresh (no GitHub API calls).

    Bypasses cooldown entirely since local state is cheap to fetch.
    """
    if state.refresh_future is not None:
        return
    if state.executor is None:
        state.executor = ThreadPoolExecutor(max_workers=1)
    state.footer_left_attr.set_attr_map({None: "footer"})
    state.spinner_index = 0
    state.refresh_is_local_only = True
    state.refresh_future = state.executor.submit(
        fetch_agent_snapshot, state.mng_ctx, state.include_filters, state.exclude_filters
    )
    _schedule_spinner_tick(loop, state)


def _start_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Start a full background refresh and begin the spinner animation."""
    if state.executor is None:
        state.executor = ThreadPoolExecutor(max_workers=1)
    state.footer_left_attr.set_attr_map({None: "footer"})
    state.spinner_index = 0
    state.refresh_is_local_only = False
    state.refresh_future = state.executor.submit(
        fetch_board_snapshot,
        state.mng_ctx,
        state.include_filters,
        state.exclude_filters,
        state.on_before_refresh or None,
        state.on_after_refresh or None,
        state.snapshot,
    )
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

    was_local_only = state.refresh_is_local_only
    failed = False
    try:
        new_snapshot = state.refresh_future.result()
        should_carry_forward = was_local_only or (
            not new_snapshot.prs_loaded and state.snapshot is not None and state.snapshot.prs_loaded
        )
        if should_carry_forward and state.snapshot is not None:
            new_snapshot = _carry_forward_pr_data(state.snapshot, new_snapshot)
        state.snapshot = new_snapshot
    except Exception as e:
        failed = True
        logger.debug("Refresh failed: {}", e)
        if state.snapshot is not None:
            state.snapshot = BoardSnapshot(
                entries=state.snapshot.entries,
                errors=(*state.snapshot.errors, f"Refresh failed: {e}"),
                prs_loaded=state.snapshot.prs_loaded,
                fetch_time_seconds=state.snapshot.fetch_time_seconds,
            )
    finally:
        state.refresh_future = None
        state.refresh_is_local_only = False
        # Only update last_refresh_time for full refreshes (so the full-refresh
        # cooldown isn't affected by cheap local-only refreshes)
        if not was_local_only:
            state.last_refresh_time = time.monotonic()

    _refresh_display(state)

    now = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
    if state.snapshot is not None:
        elapsed = f"{state.snapshot.fetch_time_seconds:.1f}s"
        state.steady_footer_text = f"  Last refresh: {now} (took {elapsed})"
    else:
        state.steady_footer_text = f"  Last refresh: {now}"
    state.footer_left_text.set_text(state.steady_footer_text)

    if failed:
        _request_refresh(loop, state, state.retry_cooldown_seconds)
    elif was_local_only:
        pass
    else:
        _schedule_next_refresh(loop, state)


@pure
def _carry_forward_pr_data(old: BoardSnapshot, new: BoardSnapshot) -> BoardSnapshot:
    """Carry forward PR data from a previous snapshot when the new one failed to load PRs.

    Matches entries by agent name and copies pr and create_pr_url from the old snapshot.
    The new snapshot's prs_loaded is set to True since we're using valid (stale) data.
    """
    old_by_name = {entry.name: entry for entry in old.entries}
    updated_entries = []
    for entry in new.entries:
        old_entry = old_by_name.get(entry.name)
        if old_entry is not None and (old_entry.pr is not None or old_entry.create_pr_url is not None):
            ref = entry.field_ref()
            updated = entry.model_copy_update(
                to_update(ref.pr, old_entry.pr),
                to_update(ref.create_pr_url, old_entry.create_pr_url),
            )
            updated_entries.append(updated)
        else:
            updated_entries.append(entry)
    return BoardSnapshot(
        entries=tuple(updated_entries),
        errors=new.errors,
        prs_loaded=True,
        fetch_time_seconds=new.fetch_time_seconds,
    )


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


def _get_state_attr(entry: AgentBoardEntry) -> str:
    """Determine the color attribute for an agent's lifecycle state.

    RUNNING gets green, WAITING gets magenta (needs user response).
    Everything else is default (no color). Muted override is handled
    separately in _build_agent_row.
    """
    if entry.state == AgentLifecycleState.RUNNING:
        return "state_running"
    if entry.state == AgentLifecycleState.WAITING:
        return "state_attention"
    return ""


def _get_name_cell_text(entry: AgentBoardEntry) -> str:
    """Get plain text for the name column cell."""
    return f"  {entry.name}"


def _get_state_cell_text(entry: AgentBoardEntry) -> str:
    """Get plain text for the state column cell."""
    return str(entry.state)


def _get_state_cell_markup(entry: AgentBoardEntry) -> str | tuple[Hashable, str]:
    """Build urwid text markup for the state column cell.

    RUNNING gets green, WAITING gets magenta. Everything else uses default color.
    """
    text = _get_state_cell_text(entry)
    attr = _get_state_attr(entry)
    return (attr, text) if attr else text


def _get_check_cell_text(entry: AgentBoardEntry) -> str:
    """Get plain text for the CI check status column cell."""
    if entry.pr is None or entry.pr.check_status == CheckStatus.UNKNOWN:
        return ""
    return entry.pr.check_status.lower()


def _get_check_cell_markup(entry: AgentBoardEntry) -> str | tuple[Hashable, str]:
    """Build urwid text markup for a CI check status column cell.

    Only failing and pending checks get color. Passing checks use default color.
    """
    if entry.pr is None or entry.pr.check_status == CheckStatus.UNKNOWN:
        return ""
    check_attr = _CHECK_STATUS_ATTR.get(entry.pr.check_status)
    if check_attr is not None:
        return (check_attr, entry.pr.check_status.lower())
    return entry.pr.check_status.lower()


def _get_push_cell_text(entry: AgentBoardEntry) -> str:
    """Get plain text for the git push status column cell."""
    if entry.work_dir is None:
        return ""
    if entry.commits_ahead is None:
        return "[not pushed]"
    if entry.commits_ahead == 0:
        return "[up to date]"
    return f"[{entry.commits_ahead} unpushed]"


def _flatten_markup_to_muted(
    markup: str | tuple[Hashable, str] | list[str | tuple[Hashable, str]],
) -> tuple[Hashable, str]:
    """Flatten rich urwid text markup to a plain string wrapped in the 'muted' attribute."""
    if isinstance(markup, list):
        plain = "".join(seg if isinstance(seg, str) else seg[1] for seg in markup)
    elif isinstance(markup, tuple):
        plain = markup[1]
    else:
        plain = markup
    return ("muted", plain)


def _get_name_cell_markup(
    entry: AgentBoardEntry, mark_key: str | None = None
) -> str | tuple[Hashable, str] | list[str | tuple[Hashable, str]]:
    """Build urwid text markup for the name column cell, with optional mark indicator."""
    if mark_key is not None:
        return [(f"mark_{mark_key}", mark_key), f" {entry.name}"]
    return f"  {entry.name}"


def _get_pr_cell_text(entry: AgentBoardEntry) -> str:
    """Get plain text for the PR column cell."""
    if entry.pr is not None:
        return f"#{entry.pr.number}"
    return ""


def _get_link_cell_text(entry: AgentBoardEntry) -> str:
    """Get plain text for the link column cell."""
    if entry.pr is not None:
        return entry.pr.url
    if entry.create_pr_url is not None:
        return entry.create_pr_url
    return ""


class _ColumnDef(FrozenModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    header: str
    text_fn: Callable[[AgentBoardEntry], str]
    markup_fn: Callable[[AgentBoardEntry], str | tuple[Hashable, str]]
    flexible: bool


def _custom_col_text(
    entry: AgentBoardEntry, col_key: str, plugin_name: str | None, field: str | None, source: str = "labels"
) -> str:
    """Get the text value for a custom column from the configured source."""
    if source == "labels":
        return entry.column_data.labels.get(col_key, "")
    return str(entry.column_data.plugin_data.get(plugin_name or "", {}).get(field or "", ""))


def _custom_col_markup(
    entry: AgentBoardEntry,
    col_key: str,
    plugin_name: str | None,
    field: str | None,
    colors: dict[str, str],
    source: str = "labels",
) -> str | tuple[Hashable, str]:
    """Get markup for a custom column, applying color when configured."""
    value = _custom_col_text(entry, col_key, plugin_name, field, source=source)
    if not value:
        return ""
    if value in colors:
        return (f"col_{col_key}_{value}", value)
    return value


class _CustomColTextFn(FrozenModel):
    """Callable that extracts a custom column's text value from an AgentBoardEntry."""

    col_key: str
    plugin_name: str | None
    field: str | None
    source: str

    def __call__(self, entry: AgentBoardEntry) -> str:
        return _custom_col_text(entry, self.col_key, self.plugin_name, self.field, self.source)


class _CustomColMarkupFn(FrozenModel):
    """Callable that produces urwid markup for a custom column value."""

    col_key: str
    plugin_name: str | None
    field: str | None
    colors: dict[str, str]
    source: str

    def __call__(self, entry: AgentBoardEntry) -> str | tuple[Hashable, str]:
        return _custom_col_markup(entry, self.col_key, self.plugin_name, self.field, self.colors, self.source)


@pure
def _build_custom_column_defs(columns_config: dict[str, CustomColumnConfig]) -> list[_ColumnDef]:
    """Build _ColumnDef list from custom column configuration."""
    defs: list[_ColumnDef] = []
    for col_key, col_config in columns_config.items():
        defs.append(
            _ColumnDef(
                name=f"custom_{col_key}",
                header=col_config.header,
                text_fn=_CustomColTextFn(
                    col_key=col_key,
                    plugin_name=col_config.plugin_name,
                    field=col_config.field,
                    source=col_config.source,
                ),
                markup_fn=_CustomColMarkupFn(
                    col_key=col_key,
                    plugin_name=col_config.plugin_name,
                    field=col_config.field,
                    colors=col_config.colors,
                    source=col_config.source,
                ),
                flexible=False,
            )
        )
    return defs


@pure
def _assemble_column_defs(
    builtin_defs: list[_ColumnDef],
    custom_defs: list[_ColumnDef],
    column_order: list[str] | None,
) -> list[_ColumnDef]:
    """Assemble the final ordered list of column definitions.

    If column_order is None, default ordering is: builtins (except last) + custom + last builtin.
    If column_order is provided, definitions are returned in that order (unknown names skipped).
    The last column in the result always gets flexible=True.
    """
    if column_order is None:
        if not custom_defs:
            return builtin_defs
        result = builtin_defs[:-1] + custom_defs + [builtin_defs[-1]]
    else:
        registry: dict[str, _ColumnDef] = {d.name: d for d in builtin_defs + custom_defs}
        result = [registry[name] for name in column_order if name in registry]
    if not result:
        return builtin_defs
    # Ensure all are non-flexible except the last
    result = [d.model_copy(update={"flexible": False}) if d.flexible else d for d in result[:-1]] + [
        result[-1].model_copy(update={"flexible": True}) if not result[-1].flexible else result[-1]
    ]
    return result


@pure
def _build_column_palette(
    columns_config: dict[str, CustomColumnConfig],
) -> tuple[list[tuple[str, str, str]], tuple[str, ...]]:
    """Build palette entries and attr names for custom column colors."""
    entries: list[tuple[str, str, str]] = []
    attr_names: list[str] = []
    for col_key, col_config in columns_config.items():
        for value, color in col_config.colors.items():
            attr = f"col_{col_key}_{value}"
            entries.append((attr, color, ""))
            entries.append((f"{attr}_focus", f"{color},standout", ""))
            attr_names.append(attr)
    return entries, tuple(attr_names)


# Single source of truth for all board column definitions (order matters)
_BOARD_COLUMN_DEFS: list[_ColumnDef] = [
    _ColumnDef(
        name="name", header="  NAME", text_fn=_get_name_cell_text, markup_fn=_get_name_cell_text, flexible=False
    ),
    _ColumnDef(
        name="state", header="STATE", text_fn=_get_state_cell_text, markup_fn=_get_state_cell_markup, flexible=False
    ),
    _ColumnDef(name="git", header="GIT", text_fn=_get_push_cell_text, markup_fn=_get_push_cell_text, flexible=False),
    _ColumnDef(name="pr", header="PR", text_fn=_get_pr_cell_text, markup_fn=_get_pr_cell_text, flexible=False),
    _ColumnDef(name="ci", header="CI", text_fn=_get_check_cell_text, markup_fn=_get_check_cell_markup, flexible=False),
    _ColumnDef(name="link", header="LINK", text_fn=_get_link_cell_text, markup_fn=_get_link_cell_text, flexible=True),
]


def _compute_board_column_widths(
    entries: tuple[AgentBoardEntry, ...],
    column_defs: list[_ColumnDef],
) -> dict[str, int]:
    """Compute column widths based on content, like tabulate auto-sizing.

    Each column is sized to fit the widest value (or header), with the
    last column (link) left flexible to fill remaining terminal space.
    """
    return {
        defn.name: max(len(defn.header), *(len(defn.text_fn(e)) for e in entries)) if entries else len(defn.header)
        for defn in column_defs
        if not defn.flexible
    }


def _build_column_header(
    widths: dict[str, int],
    column_defs: list[_ColumnDef],
) -> Columns:
    """Build the column header row for the board."""
    cols: list[tuple[int, Text] | Text] = []
    for defn in column_defs:
        if defn.flexible:
            cols.append(Text(defn.header))
        else:
            cols.append((widths[defn.name], Text(defn.header)))
    return Columns(cols, dividechars=_COL_DIVIDER_CHARS)


def _build_agent_row(
    entry: AgentBoardEntry,
    section: BoardSection,
    widths: dict[str, int],
    column_defs: list[_ColumnDef],
    mark: str | None = None,
) -> _SelectableRow:
    """Build a columnar urwid widget for a single agent row.

    Muted agents are rendered entirely in gray. Mark indicators (d/p) replace
    the leading spaces in the name column.
    """
    raw_markup: dict[str, str | tuple[Hashable, str] | list[str | tuple[Hashable, str]]] = {
        defn.name: defn.markup_fn(entry) for defn in column_defs
    }
    raw_markup["name"] = _get_name_cell_markup(entry, mark)

    # Muted agents: flatten all markup to gray
    if section == BoardSection.MUTED:
        cell_markup: dict[str, str | tuple[Hashable, str] | list[str | tuple[Hashable, str]]] = {
            k: _flatten_markup_to_muted(v) for k, v in raw_markup.items()
        }
    else:
        cell_markup = raw_markup

    cols: list[tuple[int, Text] | Text] = []
    for defn in column_defs:
        widget = Text(cell_markup[defn.name])
        if defn.flexible:
            cols.append(widget)
        else:
            cols.append((widths[defn.name], widget))
    return _SelectableRow(cols, dividechars=_COL_DIVIDER_CHARS)


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


@pure
def _build_board_widgets(
    snapshot: BoardSnapshot | None,
    column_defs: list[_ColumnDef],
    marks: dict[AgentName, str] | None = None,
    mark_attr_names: tuple[str, ...] = (),
    col_attr_names: tuple[str, ...] = (),
) -> tuple[SimpleFocusListWalker[AttrMap | Text | Divider | Columns], dict[int, AgentBoardEntry]]:
    """Build the urwid widget list from a BoardSnapshot, grouped by PR state.

    Returns (walker, index_to_entry) where index_to_entry maps list walker
    indices to the AgentBoardEntry for selectable rows.
    """
    index_to_entry: dict[int, AgentBoardEntry] = {}
    walker: SimpleFocusListWalker[AttrMap | Text | Divider | Columns] = SimpleFocusListWalker([])

    if snapshot is None:
        walker.append(Text("Loading..."))
        return walker, index_to_entry

    # Compute column widths from all entries (content-aware sizing)
    col_widths = _compute_board_column_widths(snapshot.entries, column_defs)

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

        # Add column header before the first section
        if not has_content:
            walker.append(_build_column_header(col_widths, column_defs))
        else:
            walker.append(Divider())

        if section == BoardSection.STILL_COOKING and not snapshot.prs_loaded:
            section_attr = _SECTION_ATTR[section]
            heading: list[str | tuple[Hashable, str]] = [
                (section_attr, "In progress"),
                f" - PRs not loaded ({len(entries)})",
            ]
        else:
            heading = _format_section_heading(section, len(entries))
        walker.append(Text(heading))
        has_content = True

        for entry in entries:
            mark = marks.get(entry.name) if marks else None
            item = _build_agent_row(entry, section, col_widths, column_defs, mark)
            idx = len(walker)
            focus_map: dict[str | None, str] = {None: "reversed"}
            for attr in _AGENT_LINE_ATTRS + mark_attr_names + col_attr_names:
                focus_map[attr] = f"{attr}_focus"
            walker.append(AttrMap(item, None, focus_map=focus_map))
            index_to_entry[idx] = entry

    if not has_content:
        walker.append(Text("No agents found."))

    # Show errors if any
    if snapshot.errors:
        walker.append(Divider())
        walker.append(Text(("error_text", "Errors:")))
        for error in snapshot.errors:
            walker.append(Text(("error_text", f"  {error}")))

    return walker, index_to_entry


def _refresh_display(state: _KanpanState) -> None:
    """Rebuild the body display from the current snapshot.

    Preserves focus on the previously selected agent if it still exists.
    """
    # Save the currently focused agent name before rebuilding
    focused_entry = _get_focused_entry(state)
    if focused_entry is not None:
        state.focused_agent_name = focused_entry.name

    walker, state.index_to_entry = _build_board_widgets(
        state.snapshot,
        state.column_defs,
        state.marks or None,
        state.mark_attr_names,
        state.col_attr_names,
    )
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
    loop.set_alarm_in(state.refresh_interval_seconds, _on_auto_refresh_alarm, state)


def _on_auto_refresh_alarm(loop: MainLoop, state: _KanpanState) -> None:
    """Alarm callback for periodic auto-refresh."""
    if state.refresh_future is None:
        _start_refresh(loop, state)


def _load_refresh_hooks(hooks_raw: dict[str, Any]) -> list[RefreshHook]:
    """Parse and filter enabled refresh hooks from a raw config dict.

    Config loader uses model_construct() which bypasses validation,
    so nested dicts may not be parsed into RefreshHook objects.
    """
    hooks: list[RefreshHook] = []
    for value in hooks_raw.values():
        if isinstance(value, RefreshHook):
            hook = value
        elif isinstance(value, dict):
            hook = RefreshHook(**value)
        else:
            continue
        if hook.enabled:
            hooks.append(hook)
    return hooks


def _load_user_commands(mng_ctx: MngContext) -> dict[str, CustomCommand]:
    """Load user-defined commands from plugin config."""
    config = mng_ctx.get_plugin_config("kanpan", KanpanPluginConfig)
    # Config loader uses model_construct() which bypasses validation,
    # so nested dicts may not be parsed into CustomCommand objects.
    result: dict[str, CustomCommand] = {}
    for key, value in config.commands.items():
        if isinstance(value, CustomCommand):
            result[key] = value
        elif isinstance(value, dict):
            result[key] = CustomCommand(**value)
    return result


def _load_user_columns(mng_ctx: MngContext) -> dict[str, CustomColumnConfig]:
    """Load user-defined custom columns from plugin config."""
    config = mng_ctx.get_plugin_config("kanpan", KanpanPluginConfig)
    result: dict[str, CustomColumnConfig] = {}
    for key, value in config.columns.items():
        if isinstance(value, CustomColumnConfig):
            result[key] = value
        elif isinstance(value, dict):
            result[key] = CustomColumnConfig(**value)
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


@pure
def _build_mark_palette(
    commands: dict[str, CustomCommand],
) -> tuple[list[tuple[str, str, str]], tuple[str, ...]]:
    """Build palette entries and attr names for markable commands.

    Returns (palette_entries, mark_attr_names).
    """
    entries: list[tuple[str, str, str]] = []
    attr_names: list[str] = []
    for key, cmd in commands.items():
        if not cmd.markable:
            continue
        color = cmd.markable if isinstance(cmd.markable, str) else _DEFAULT_MARK_COLOR
        attr = f"mark_{key}"
        entries.append((attr, color, ""))
        entries.append((f"{attr}_focus", f"{color},standout", ""))
        attr_names.append(attr)
    return entries, tuple(attr_names)


def run_kanpan(
    mng_ctx: MngContext,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> None:  # pragma: no cover
    """Run the kanpan TUI board."""
    commands = _build_command_map(mng_ctx)
    plugin_config = mng_ctx.get_plugin_config("kanpan", KanpanPluginConfig)

    # Build footer keybindings, visually separating mark-related from action commands
    mark_keys = {_BUILTIN_COMMAND_KEY_UNMARK}
    mark_parts = [f"{key}: {cmd.name}" for key, cmd in commands.items() if cmd.markable or key in mark_keys]
    mark_parts.append("U: unmark all")
    action_parts = [f"{key}: {cmd.name}" for key, cmd in commands.items() if not cmd.markable and key not in mark_keys]
    action_parts.append("q: quit")
    keybindings = "  ".join(mark_parts + ["|"] + action_parts) + "  "

    footer_left_text = Text("  Loading...")
    footer_left_attr = AttrMap(footer_left_text, "footer")
    footer_right = Text(keybindings, align="right")
    # Pack the left side so it gets exactly the space its text needs; the right
    # side (keybindings) gets the remainder and wraps when the terminal is narrow.
    footer_items: list[Any] = [("pack", footer_left_attr), AttrMap(footer_right, "footer")]
    footer_columns = Columns(footer_items, dividechars=1)
    footer = Pile([Divider(), footer_columns])

    is_filtered = bool(include_filters or exclude_filters)
    header_title = "Kanpan - all-seeing agent tracker - 看 πᾶν"
    if is_filtered:
        header_title += "  [filtered]"
    header = Pile(
        [
            AttrMap(Text(header_title, align="center"), "header"),
            Divider(),
        ]
    )

    initial_body = Filler(Pile([Text("Loading...")]), valign="top")
    frame = Frame(body=initial_body, header=header, footer=footer)

    mark_palette_entries, mark_attr_names = _build_mark_palette(commands)

    # Build custom column definitions
    user_columns = _load_user_columns(mng_ctx)
    custom_col_defs = _build_custom_column_defs(user_columns)
    column_defs = _assemble_column_defs(_BOARD_COLUMN_DEFS, custom_col_defs, plugin_config.column_order)
    col_palette_entries, col_attr_names = _build_column_palette(user_columns)

    on_before_refresh = _load_refresh_hooks(plugin_config.on_before_refresh)
    on_after_refresh = _load_refresh_hooks(plugin_config.on_after_refresh)

    state = _KanpanState(
        mng_ctx=mng_ctx,
        frame=frame,
        footer_left_text=footer_left_text,
        footer_left_attr=footer_left_attr,
        footer_right=footer_right,
        commands=commands,
        on_before_refresh=on_before_refresh,
        on_after_refresh=on_after_refresh,
        refresh_interval_seconds=plugin_config.refresh_interval_seconds,
        retry_cooldown_seconds=plugin_config.retry_cooldown_seconds,
        mark_attr_names=mark_attr_names,
        column_defs=column_defs,
        col_attr_names=col_attr_names,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
    )

    input_handler = _KanpanInputHandler(state=state)

    screen = Screen()
    screen.tty_signal_keys(intr="undefined")

    loop = MainLoop(
        frame,
        palette=PALETTE + mark_palette_entries + col_palette_entries,
        unhandled_input=input_handler,
        screen=screen,
    )
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
