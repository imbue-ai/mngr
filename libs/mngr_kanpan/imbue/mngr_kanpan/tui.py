import os
import subprocess
import time
from collections.abc import Callable
from collections.abc import Hashable
from collections.abc import Sequence
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timezone
from typing import Any

from loguru import logger
from pydantic import ConfigDict
from urwid.canvas import TextCanvas
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
from imbue.mngr.cli.urwid_utils import create_urwid_screen_preserving_terminal
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import KanpanDataSource
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import CustomCommand
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.fetcher import FetchResult
from imbue.mngr_kanpan.fetcher import collect_data_sources
from imbue.mngr_kanpan.fetcher import compute_section
from imbue.mngr_kanpan.fetcher import fetch_board_snapshot
from imbue.mngr_kanpan.fetcher import fetch_local_snapshot
from imbue.mngr_kanpan.fetcher import load_field_cache
from imbue.mngr_kanpan.fetcher import save_field_cache
from imbue.mngr_kanpan.fetcher import toggle_agent_mute

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
    ("section_prs_failed", "light red", ""),
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
    BoardSection.PR_DRAFT,
    BoardSection.STILL_COOKING,
    BoardSection.PRS_FAILED,
    BoardSection.MUTED,
)

# Section labels split into colored prefix and plain suffix
_SECTION_PREFIX: dict[BoardSection, str] = {
    BoardSection.PR_MERGED: "Done",
    BoardSection.PR_CLOSED: "Cancelled",
    BoardSection.PR_BEING_REVIEWED: "In review",
    BoardSection.PR_DRAFT: "In progress",
    BoardSection.STILL_COOKING: "In progress",
    BoardSection.PRS_FAILED: "In progress",
    BoardSection.MUTED: "Muted",
}

_SECTION_SUFFIX: dict[BoardSection, str] = {
    BoardSection.PR_MERGED: "PR merged",
    BoardSection.PR_CLOSED: "PR closed",
    BoardSection.PR_BEING_REVIEWED: "PR pending",
    BoardSection.PR_DRAFT: "draft PR",
    BoardSection.STILL_COOKING: "no PR yet",
    BoardSection.PRS_FAILED: "PRs failed",
    BoardSection.MUTED: "",
}

_SECTION_ATTR: dict[BoardSection, str] = {
    BoardSection.PR_MERGED: "section_done",
    BoardSection.PR_CLOSED: "section_cancelled",
    BoardSection.PR_BEING_REVIEWED: "section_in_review",
    BoardSection.PR_DRAFT: "section_in_progress",
    BoardSection.STILL_COOKING: "section_in_progress",
    BoardSection.PRS_FAILED: "section_prs_failed",
    BoardSection.MUTED: "section_muted",
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


def _osc8_wrap_content(inner_content: Any, osc_open: bytes, osc_close: bytes) -> Any:
    """Wrap each row of canvas content with OSC 8 open/close escape sequences.

    Only wraps the visible text, not trailing whitespace padding, so the
    terminal hyperlink underline doesn't extend across the full column width.

    Sets the charset to "U" on modified segments so that urwid's Screen skips
    the UNPRINTABLE_TRANS_TABLE translation (which would replace ESC bytes with
    '?'). On UTF-8 terminals the "U" charset flag has no other effect.
    """
    for row in inner_content:
        if not row:
            yield row
            continue
        new_row = [*row]
        # Insert osc_close before trailing padding in the last segment
        last = new_row[-1]
        last_text: Any = last[2]
        stripped = last_text.rstrip(b" ")
        padding = last_text[len(stripped) :]
        new_row[-1] = (last[0], "U", stripped + osc_close + padding)
        # Prepend osc_open to the first segment
        first = new_row[0]
        new_row[0] = (first[0], "U", osc_open + first[2])
        yield new_row


class _HyperlinkCanvas(MutableModel):
    """Canvas wrapper that injects OSC 8 terminal hyperlink escape sequences."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    inner: TextCanvas
    url: str
    _widget_info: Any = None
    cacheable: bool = False

    @property
    def widget_info(self) -> Any:
        return self._widget_info

    @property
    def coords(self) -> dict[str, Any]:
        return self.inner.coords

    @property
    def shortcuts(self) -> dict[str, str]:
        return self.inner.shortcuts

    @property
    def text(self) -> list[bytes]:
        return self.inner.text

    @property
    def cursor(self) -> tuple[int, int] | None:
        return None

    def finalize(self, widget: Any, size: Any, focus: bool) -> None:
        self._widget_info = (widget, size, focus)

    def rows(self) -> int:
        return self.inner.rows()

    def cols(self) -> int:
        return self.inner.cols()

    def translate_coords(self, dx: int, dy: int) -> dict[str, Any]:
        return self.inner.translate_coords(dx, dy)

    def content(
        self, trim_left: int = 0, trim_top: int = 0, cols: int | None = 0, rows: int | None = 0, attr: Any = None
    ) -> Any:
        osc_open = f"\033]8;;{self.url}\033\\".encode()
        osc_close = b"\033]8;;\033\\"
        return _osc8_wrap_content(self.inner.content(trim_left, trim_top, cols, rows, attr), osc_open, osc_close)

    def content_delta(self, other: Any) -> Any:
        return self.content()


class _HyperlinkText(Text):
    """Text widget that wraps its rendered content in an OSC 8 terminal hyperlink."""

    _hyperlink_url: str = ""

    def render(self, size: tuple[int] | tuple[()], focus: bool = False) -> Any:
        canvas = super().render(size, focus)
        if not self._hyperlink_url:
            return canvas
        return _HyperlinkCanvas(inner=canvas, url=self._hyperlink_url)


class _SelectableRow(Columns):
    """A Columns widget that is selectable, allowing it to receive focus."""

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple[()] | tuple[int] | tuple[int, int], key: str) -> str | None:
        """Pass all keys through (no keys are handled by this widget)."""
        return key


class _KanpanState(MutableModel):
    """Mutable state for the kanpan TUI."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    mngr_ctx: MngrContext
    snapshot: BoardSnapshot | None = None
    frame: Any  # urwid Frame widget
    footer_left_text: Any  # urwid Text widget (left side of footer)
    footer_left_attr: Any  # urwid AttrMap wrapping footer_left_text
    footer_right: Any  # urwid Text widget (right side of footer)
    loop: Any = None  # urwid MainLoop, set after construction
    spinner_index: int = 0
    refresh_future: Future[FetchResult] | None = None
    # In-memory cache of fields from previous refresh cycle
    cached_fields: dict[AgentName, dict[str, FieldValue]] = {}
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
    # Column definitions (from data sources)
    column_defs: list["_ColumnDef"] = []
    # Board section display order (from config or default BOARD_SECTION_ORDER)
    section_order: tuple[BoardSection, ...] = BOARD_SECTION_ORDER
    # Palette attr names for custom column colors
    col_attr_names: tuple[str, ...] = ()
    # Data sources collected from plugins
    data_sources: Sequence[KanpanDataSource] = ()
    # CEL filter expressions passed from CLI
    include_filters: tuple[str, ...] = ()
    exclude_filters: tuple[str, ...] = ()


class _KanpanInputHandler(MutableModel):
    """Callable input handler for the kanpan TUI."""

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
        state.list_walker.set_focus(0)


def _get_focused_entry(state: _KanpanState) -> AgentBoardEntry | None:
    """Get the AgentBoardEntry of the currently focused entry, or None."""
    if state.list_walker is None:
        return None
    _, focus_index = state.list_walker.get_focus()
    if focus_index is None:
        return None
    return state.index_to_entry.get(focus_index)


def _run_destroy(agent_names: list[str]) -> subprocess.CompletedProcess[str]:  # pragma: no cover
    """Run mngr destroy in a subprocess. Called from a background thread."""
    return subprocess.run(
        ["mngr", "destroy", *agent_names, "--force"],
        capture_output=True,
        text=True,
        timeout=60,
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
    name_markup: str | tuple[Hashable, str] | list[str | tuple[Hashable, str]] = _get_name_cell_markup(entry, mark_key)
    if entry.section == BoardSection.MUTED:
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
    marked_names = set(state.marks.keys())
    state.marks.clear()
    for idx, entry in state.index_to_entry.items():
        if entry.name in marked_names:
            _update_row_mark(state, idx, None)
    _update_mark_count_footer(state)


def _prune_orphaned_marks(state: _KanpanState) -> None:
    """Remove marks for agents that are no longer in the current snapshot."""
    if state.snapshot is None or not state.marks:
        return
    current_names = {e.name for e in state.snapshot.entries}
    orphaned = [name for name in state.marks if name not in current_names]
    for name in orphaned:
        del state.marks[name]
    if orphaned:
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
    batch_names: tuple[AgentName, ...] = ()


@pure
def _batch_item_label(item: _BatchWorkItem) -> str:
    """Format a human-readable label for a batch work item."""
    if item.batch_names:
        return f"{item.cmd.name} {len(item.batch_names)} agent(s)"
    return f"{item.cmd.name} {item.name}"


def _run_shell_command_sync(command: str, agent_name: str) -> subprocess.CompletedProcess[str]:
    """Run a shell command with MNGR_AGENT_NAME set. Called from a background thread."""
    env = {**os.environ, "MNGR_AGENT_NAME": agent_name}
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

    delete_names: list[AgentName] = []
    individual_work: list[_BatchWorkItem] = []
    for name, mark_key in state.marks.items():
        cmd = state.commands.get(mark_key)
        if cmd is None:
            continue
        if mark_key == _BUILTIN_COMMAND_KEY_DELETE:
            delete_names.append(name)
        else:
            individual_work.append(_BatchWorkItem(name=name, key=mark_key, cmd=cmd, entry=entries_by_name.get(name)))

    work: list[_BatchWorkItem] = []
    if delete_names:
        delete_cmd = state.commands.get(_BUILTIN_COMMAND_KEY_DELETE)
        if delete_cmd is not None:
            work.append(
                _BatchWorkItem(
                    name=delete_names[0],
                    key=_BUILTIN_COMMAND_KEY_DELETE,
                    cmd=delete_cmd,
                    entry=entries_by_name.get(delete_names[0]),
                    batch_names=tuple(delete_names),
                )
            )
    work.extend(individual_work)

    _execute_next_in_batch(state, work, [], 0)


def _submit_batch_item(
    executor: ThreadPoolExecutor, item: _BatchWorkItem
) -> Future[subprocess.CompletedProcess[str]] | None:
    """Submit a single batch work item to the executor."""
    if item.key == _BUILTIN_COMMAND_KEY_DELETE:
        names = [str(n) for n in item.batch_names] if item.batch_names else [str(item.name)]
        return executor.submit(_run_destroy, names)
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
        results.append(f"{_batch_item_label(item)}: skipped (not executable)")
        _execute_next_in_batch(state, work, results, index + 1)
        return

    state.footer_left_text.set_text(f"{state.execute_status}{_batch_item_label(item)}...")

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
        label = _batch_item_label(item)
        try:
            result = future.result()
            if result.returncode == 0:
                if item.batch_names:
                    for n in item.batch_names:
                        results.append(f"{item.cmd.name} {n}: ok")
                        state.marks.pop(n, None)
                else:
                    results.append(f"{item.cmd.name} {item.name}: ok")
                    state.marks.pop(item.name, None)
            else:
                stderr = result.stderr.strip()
                results.append(f"{label}: failed ({stderr})")
        except Exception as e:
            results.append(f"{label}: failed ({e})")

        _execute_next_in_batch(state, work, results, index + 1)
        return

    frame_char = SPINNER_FRAMES[state.spinner_index % len(SPINNER_FRAMES)]
    state.footer_left_text.set_text(f"{state.execute_status}{_batch_item_label(item)} {frame_char}")
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

    # Local-only refresh to immediately show updated state
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
    state.snapshot = state.snapshot.model_copy_update(
        to_update(state.snapshot.field_ref().entries, new_entries),
    )


def _mute_focused_agent(state: _KanpanState) -> None:
    """Toggle mute on the currently focused agent."""
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
        return toggle_agent_mute(state.mngr_ctx, agent_name)

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
    """Dispatch a command by key."""
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
        env = {**os.environ, "MNGR_AGENT_NAME": str(agent_name)}
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
    """Request a refresh, subject to a cooldown period."""
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
    """Start a local-only background refresh (no GitHub API calls)."""
    if state.refresh_future is not None:
        return
    if state.executor is None:
        state.executor = ThreadPoolExecutor(max_workers=1)
    state.footer_left_attr.set_attr_map({None: "footer"})
    state.spinner_index = 0
    state.refresh_is_local_only = True
    state.refresh_future = state.executor.submit(
        fetch_local_snapshot,
        state.mngr_ctx,
        state.data_sources,
        state.cached_fields,
        state.include_filters,
        state.exclude_filters,
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
        state.mngr_ctx,
        state.data_sources,
        state.cached_fields,
        state.include_filters,
        state.exclude_filters,
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
        fetch_result = state.refresh_future.result()
        new_snapshot = fetch_result.snapshot
        # Update in-memory field cache
        state.cached_fields = fetch_result.cached_fields
        # Persist cache to disk after full refreshes
        if not was_local_only:
            save_field_cache(state.mngr_ctx, state.cached_fields, state.data_sources)
        # For local-only refreshes, carry forward fields from previous snapshot
        if was_local_only and state.snapshot is not None:
            new_snapshot = _carry_forward_fields(state.snapshot, new_snapshot)
        state.snapshot = new_snapshot
    except Exception as e:
        failed = True
        logger.debug("Refresh failed: {}", e)
        if state.snapshot is not None:
            state.snapshot = state.snapshot.model_copy_update(
                to_update(
                    state.snapshot.field_ref().errors,
                    (*state.snapshot.errors, f"Refresh failed: {e}"),
                ),
            )
    finally:
        state.refresh_future = None
        state.refresh_is_local_only = False
        if not was_local_only:
            state.last_refresh_time = time.monotonic()

    _refresh_display(state)
    _prune_orphaned_marks(state)

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
def _carry_forward_fields(old: BoardSnapshot, new: BoardSnapshot) -> BoardSnapshot:
    """Carry forward field data from a previous full snapshot for local-only refreshes.

    Local-only refreshes only run git_info and repo_paths. Other fields (PR, CI, etc.)
    are carried forward from the previous snapshot.
    """
    old_by_name = {entry.name: entry for entry in old.entries}
    updated_entries: list[AgentBoardEntry] = []
    for entry in new.entries:
        old_entry = old_by_name.get(entry.name)
        if old_entry is not None:
            # Merge: new fields override old, but keep old fields not produced by local sources
            merged_fields = dict(old_entry.fields)
            merged_fields.update(entry.fields)
            merged_cells = {key: field.display() for key, field in merged_fields.items()}
            section = compute_section(merged_fields)
            ref = entry.field_ref()
            updated = entry.model_copy_update(
                to_update(ref.fields, merged_fields),
                to_update(ref.cells, merged_cells),
                to_update(ref.section, section),
            )
            updated_entries.append(updated)
        else:
            updated_entries.append(entry)
    return BoardSnapshot(
        entries=tuple(updated_entries),
        errors=new.errors,
        fetch_time_seconds=new.fetch_time_seconds,
    )


def _get_state_attr(entry: AgentBoardEntry) -> str:
    """Determine the color attribute for an agent's lifecycle state."""
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
    """Build urwid text markup for the state column cell."""
    text = _get_state_cell_text(entry)
    attr = _get_state_attr(entry)
    return (attr, text) if attr else text


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


def _field_cell_text(entry: AgentBoardEntry, field_key: str) -> str:
    """Get plain text for a field-based column cell."""
    cell = entry.cells.get(field_key)
    if cell is None:
        return ""
    return cell.text


def _field_cell_markup(entry: AgentBoardEntry, field_key: str) -> str | tuple[Hashable, str]:
    """Build urwid text markup for a field-based column cell."""
    cell = entry.cells.get(field_key)
    if cell is None:
        return ""
    if cell.color is not None:
        return (f"field_{field_key}_{cell.color}", cell.text)
    return cell.text


class _ColumnDef(FrozenModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    header: str
    text_fn: Callable[[AgentBoardEntry], str]
    markup_fn: Callable[[AgentBoardEntry], str | tuple[Hashable, str]]
    flexible: bool


class _FieldCellTextFn(FrozenModel):
    """Callable that extracts a field cell's text from an AgentBoardEntry."""

    field_key: str

    def __call__(self, entry: AgentBoardEntry) -> str:
        return _field_cell_text(entry, self.field_key)


class _FieldCellMarkupFn(FrozenModel):
    """Callable that produces urwid markup for a field cell."""

    field_key: str

    def __call__(self, entry: AgentBoardEntry) -> str | tuple[Hashable, str]:
        return _field_cell_markup(entry, self.field_key)


# Built-in column definitions for name and state (always present)
_BUILTIN_COLUMN_DEFS: list[_ColumnDef] = [
    _ColumnDef(
        name="name", header="  NAME", text_fn=_get_name_cell_text, markup_fn=_get_name_cell_text, flexible=False
    ),
    _ColumnDef(
        name="state", header="STATE", text_fn=_get_state_cell_text, markup_fn=_get_state_cell_markup, flexible=False
    ),
]


@pure
def _build_data_source_column_defs(
    data_sources: Sequence[KanpanDataSource],
) -> list[_ColumnDef]:
    """Build column definitions from data source declarations."""
    defs: list[_ColumnDef] = []
    seen: set[str] = set()
    for source in data_sources:
        for field_key, header in source.columns.items():
            if field_key in seen:
                continue
            seen.add(field_key)
            # Skip empty headers (infrastructure columns like create_pr_url)
            if not header:
                continue
            defs.append(
                _ColumnDef(
                    name=field_key,
                    header=header,
                    text_fn=_FieldCellTextFn(field_key=field_key),
                    markup_fn=_FieldCellMarkupFn(field_key=field_key),
                    flexible=False,
                )
            )
    return defs


@pure
def _assemble_column_defs(
    builtin_defs: list[_ColumnDef],
    source_defs: list[_ColumnDef],
    column_order: list[str] | None,
) -> list[_ColumnDef]:
    """Assemble the final ordered list of column definitions.

    If column_order is None, default ordering is: builtins + source columns.
    If column_order is provided, definitions are returned in that order.
    The last column always gets flexible=True.
    """
    if column_order is None:
        result = builtin_defs + source_defs
    else:
        registry: dict[str, _ColumnDef] = {d.name: d for d in builtin_defs + source_defs}
        result = [registry[name] for name in column_order if name in registry]
    if not result:
        return builtin_defs
    # Ensure all are non-flexible except the last
    result = [d.model_copy(update={"flexible": False}) if d.flexible else d for d in result[:-1]] + [
        result[-1].model_copy(update={"flexible": True}) if not result[-1].flexible else result[-1]
    ]
    return result


@pure
def _resolve_section_order(
    config_order: list[BoardSection] | None,
) -> tuple[BoardSection, ...]:
    """Resolve the configured section order, falling back to the default."""
    if config_order is None:
        return BOARD_SECTION_ORDER
    return tuple(config_order)


@pure
def _build_field_color_palette(
    snapshot: BoardSnapshot | None,
) -> tuple[list[tuple[str, str, str]], tuple[str, ...]]:
    """Build palette entries for field-based column colors.

    Scans all cells in the snapshot for colors and creates palette entries.
    """
    entries: list[tuple[str, str, str]] = []
    attr_names: list[str] = []
    seen: set[str] = set()

    if snapshot is None:
        return entries, tuple(attr_names)

    for entry in snapshot.entries:
        for field_key, cell in entry.cells.items():
            if cell.color is not None:
                attr = f"field_{field_key}_{cell.color}"
                if attr not in seen:
                    seen.add(attr)
                    entries.append((attr, cell.color, ""))
                    entries.append((f"{attr}_focus", f"{cell.color},standout", ""))
                    attr_names.append(attr)

    return entries, tuple(attr_names)


def _compute_board_column_widths(
    entries: tuple[AgentBoardEntry, ...],
    column_defs: list[_ColumnDef],
) -> dict[str, int]:
    """Compute column widths based on content."""
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
    widths: dict[str, int],
    column_defs: list[_ColumnDef],
    mark: str | None = None,
) -> _SelectableRow:
    """Build a columnar urwid widget for a single agent row."""
    raw_markup: dict[str, str | tuple[Hashable, str] | list[str | tuple[Hashable, str]]] = {
        defn.name: defn.markup_fn(entry) for defn in column_defs
    }
    raw_markup["name"] = _get_name_cell_markup(entry, mark)

    # Muted agents: flatten all markup to gray
    if entry.section == BoardSection.MUTED:
        cell_markup: dict[str, str | tuple[Hashable, str] | list[str | tuple[Hashable, str]]] = {
            k: _flatten_markup_to_muted(v) for k, v in raw_markup.items()
        }
    else:
        cell_markup = raw_markup

    cols: list[tuple[int, Text] | Text] = []
    for defn in column_defs:
        cell = entry.cells.get(defn.name)
        cell_url = cell.url if cell is not None else None
        if cell_url:
            hyperlink_widget = _HyperlinkText(cell_markup[defn.name])
            hyperlink_widget._hyperlink_url = cell_url
            widget = hyperlink_widget
        else:
            widget = Text(cell_markup[defn.name])
        if defn.flexible:
            cols.append(widget)
        else:
            cols.append((widths[defn.name], widget))
    return _SelectableRow(cols, dividechars=_COL_DIVIDER_CHARS)


def _format_section_heading(section: BoardSection, count: int) -> list[str | tuple[Hashable, str]]:
    """Build urwid text markup for a section heading."""
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
    section_order: tuple[BoardSection, ...] = BOARD_SECTION_ORDER,
) -> tuple[SimpleFocusListWalker[AttrMap | Text | Divider | Columns], dict[int, AgentBoardEntry]]:
    """Build the urwid widget list from a BoardSnapshot, grouped by section."""
    index_to_entry: dict[int, AgentBoardEntry] = {}
    walker: SimpleFocusListWalker[AttrMap | Text | Divider | Columns] = SimpleFocusListWalker([])

    if snapshot is None:
        walker.append(Text("Loading..."))
        return walker, index_to_entry

    # Compute column widths from all entries
    col_widths = _compute_board_column_widths(snapshot.entries, column_defs)

    # Group entries by section (pre-computed on each entry)
    by_section: dict[BoardSection, list[AgentBoardEntry]] = {}
    for entry in snapshot.entries:
        by_section.setdefault(entry.section, []).append(entry)

    has_content = False

    for section in section_order:
        entries = by_section.get(section)
        if not entries:
            continue

        # Add column header before the first section
        if not has_content:
            walker.append(_build_column_header(col_widths, column_defs))
        else:
            walker.append(Divider())

        heading = _format_section_heading(section, len(entries))
        walker.append(Text(heading))
        has_content = True

        for entry in entries:
            mark = marks.get(entry.name) if marks else None
            item = _build_agent_row(entry, col_widths, column_defs, mark)
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
    """Rebuild the body display from the current snapshot."""
    # Save the currently focused agent name before rebuilding
    focused_entry = _get_focused_entry(state)
    if focused_entry is not None:
        state.focused_agent_name = focused_entry.name

    # Update field color palette from snapshot and register new entries with the screen
    field_palette, field_attr_names = _build_field_color_palette(state.snapshot)
    state.col_attr_names = field_attr_names
    if state.loop is not None and field_palette:
        state.loop.screen.register_palette(field_palette)

    walker, state.index_to_entry = _build_board_widgets(
        state.snapshot,
        state.column_defs,
        state.marks or None,
        state.mark_attr_names,
        state.col_attr_names,
        state.section_order,
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


def _load_user_commands(mngr_ctx: MngrContext) -> dict[str, CustomCommand]:
    """Load user-defined commands from plugin config."""
    config = mngr_ctx.get_plugin_config("kanpan", KanpanPluginConfig)
    result: dict[str, CustomCommand] = {}
    for key, value in config.commands.items():
        if isinstance(value, CustomCommand):
            result[key] = value
        elif isinstance(value, dict):
            result[key] = CustomCommand(**value)
    return result


def _build_command_map(mngr_ctx: MngrContext) -> dict[str, CustomCommand]:
    """Build the unified command map: builtins merged with user config."""
    commands = dict(_BUILTIN_COMMANDS)
    user_commands = _load_user_commands(mngr_ctx)
    commands.update(user_commands)
    return {key: cmd for key, cmd in commands.items() if cmd.enabled}


@pure
def _build_mark_palette(
    commands: dict[str, CustomCommand],
) -> tuple[list[tuple[str, str, str]], tuple[str, ...]]:
    """Build palette entries and attr names for markable commands."""
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
    mngr_ctx: MngrContext,
    include_filters: tuple[str, ...] = (),
    exclude_filters: tuple[str, ...] = (),
) -> None:  # pragma: no cover
    """Run the kanpan TUI board."""
    commands = _build_command_map(mngr_ctx)
    plugin_config = mngr_ctx.get_plugin_config("kanpan", KanpanPluginConfig)

    # Collect data sources and load cached fields from disk
    data_sources = collect_data_sources(mngr_ctx)
    initial_cached_fields = load_field_cache(mngr_ctx, data_sources)

    # Build footer keybindings
    mark_keys = {_BUILTIN_COMMAND_KEY_UNMARK}
    mark_parts = [f"{key}: {cmd.name}" for key, cmd in commands.items() if cmd.markable or key in mark_keys]
    mark_parts.append("U: unmark all")
    action_parts = [f"{key}: {cmd.name}" for key, cmd in commands.items() if not cmd.markable and key not in mark_keys]
    action_parts.append("q: quit")
    keybindings = "  ".join(mark_parts + ["|"] + action_parts) + "  "

    footer_left_text = Text("  Loading...")
    footer_left_attr = AttrMap(footer_left_text, "footer")
    footer_right = Text(keybindings, align="right")
    footer_items: list[Any] = [("pack", footer_left_attr), AttrMap(footer_right, "footer")]
    footer_columns = Columns(footer_items, dividechars=1)
    footer = Pile([Divider(), footer_columns])

    is_filtered = bool(include_filters or exclude_filters)
    header_title = "Kanpan - all-seeing agent tracker - \u770b \u03c0\u1fb6\u03bd"
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

    # Build column definitions from data sources
    source_col_defs = _build_data_source_column_defs(data_sources)
    column_defs = _assemble_column_defs(_BUILTIN_COLUMN_DEFS, source_col_defs, plugin_config.column_order)

    section_order = _resolve_section_order(plugin_config.section_order)

    state = _KanpanState(
        mngr_ctx=mngr_ctx,
        frame=frame,
        footer_left_text=footer_left_text,
        footer_left_attr=footer_left_attr,
        footer_right=footer_right,
        commands=commands,
        refresh_interval_seconds=plugin_config.refresh_interval_seconds,
        retry_cooldown_seconds=plugin_config.retry_cooldown_seconds,
        mark_attr_names=mark_attr_names,
        column_defs=column_defs,
        data_sources=data_sources,
        cached_fields=initial_cached_fields,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        section_order=section_order,
    )

    input_handler = _KanpanInputHandler(state=state)

    with create_urwid_screen_preserving_terminal() as screen:
        loop = MainLoop(
            frame,
            palette=PALETTE + mark_palette_entries,
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
