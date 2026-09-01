import os
import subprocess
import time
from collections.abc import Callable
from collections.abc import Hashable
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import ConfigDict
from urwid.canvas import TextCanvas
from urwid.display.common import BaseScreen
from urwid.display.raw import Screen
from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.event_loop.main_loop import MainLoop
from urwid.signals import connect_signal
from urwid.str_util import calc_width
from urwid.util import is_mouse_press
from urwid.widget.attr_map import AttrMap
from urwid.widget.columns import Columns
from urwid.widget.constants import WrapMode
from urwid.widget.divider import Divider
from urwid.widget.edit import Edit
from urwid.widget.filler import Filler
from urwid.widget.frame import Frame
from urwid.widget.line_box import LineBox
from urwid.widget.listbox import ListBox
from urwid.widget.listbox import SimpleFocusListWalker
from urwid.widget.overlay import Overlay
from urwid.widget.padding import Padding
from urwid.widget.pile import Pile
from urwid.widget.text import Text
from urwid_readline import ReadlineEdit

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.urwid_utils import create_urwid_screen_preserving_terminal
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.logging import CLEAR_SCREEN
from imbue.mngr_kanpan.data_source import BoolField
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import CellRun
from imbue.mngr_kanpan.data_source import FIELD_MUTED
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import KanpanDataSource
from imbue.mngr_kanpan.data_source import KanpanFieldTypeError
from imbue.mngr_kanpan.data_source import now_utc
from imbue.mngr_kanpan.data_types import ActionBuiltinCommand
from imbue.mngr_kanpan.data_types import ActionBuiltinRole
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import CustomCommand
from imbue.mngr_kanpan.data_types import DEFAULT_LOCAL_REFRESH_INTERVAL_SECONDS
from imbue.mngr_kanpan.data_types import KanpanCommand
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.data_types import MarkableBuiltinCommand
from imbue.mngr_kanpan.data_types import MarkableBuiltinRole
from imbue.mngr_kanpan.data_types import SECTION_PREFIX
from imbue.mngr_kanpan.data_types import SECTION_SUFFIX
from imbue.mngr_kanpan.data_types import STALENESS_FRACTION_OF_REFRESH_INTERVAL
from imbue.mngr_kanpan.data_types import group_entries_by_section
from imbue.mngr_kanpan.fetcher import FetchResult
from imbue.mngr_kanpan.fetcher import collect_data_sources
from imbue.mngr_kanpan.fetcher import compute_section
from imbue.mngr_kanpan.fetcher import fetch_board_snapshot
from imbue.mngr_kanpan.fetcher import fetch_local_snapshot
from imbue.mngr_kanpan.fetcher import load_field_cache
from imbue.mngr_kanpan.fetcher import save_field_cache
from imbue.mngr_kanpan.fetcher import set_agent_mute
from imbue.mngr_kanpan.header_status import HeaderStatus
from imbue.mngr_kanpan.header_status import compile_header_status
from imbue.mngr_kanpan.header_status import render_header_status

DEFAULT_REFRESH_INTERVAL_SECONDS: float = 600.0
# Fallback used by the dataclass default and a couple of tests; runtime always
# resolves the threshold from KanpanPluginConfig.effective_staleness_threshold_seconds().
DEFAULT_STALENESS_THRESHOLD_SECONDS: float = STALENESS_FRACTION_OF_REFRESH_INTERVAL * DEFAULT_REFRESH_INTERVAL_SECONDS

# The column carrying the agent name, which is also where a dired-style mark renders.
_NAME_COLUMN: Final[str] = "name"

# Default column order when column_order is not explicitly configured.
# User-configured label/shell columns are appended after these.
DEFAULT_COLUMN_ORDER: tuple[str, ...] = (
    _NAME_COLUMN,
    "state",
    "commits_ahead",
    "pr",
    "ci",
    "conflicts",
    "unresolved",
)

SPINNER_FRAMES: tuple[str, ...] = ("|", "/", "-", "\\")
SPINNER_INTERVAL_SECONDS: float = 0.15
TRANSIENT_MESSAGE_SECONDS: float = 3.0

# Peek panel: how many trailing transcript lines to show, and how often to refresh
# while the panel is open.
PEEK_BODY_HEIGHT: int = 14
PEEK_REFRESH_SECONDS: float = 2.0
PEEK_REPLY_PROMPT: str = "› "

# Width of the centred prompt box. Fixed rather than a fraction of the terminal so one
# short line of input does not stretch across a wide screen; urwid clips it on narrower ones.
_PROMPT_WIDTH: int = 56
HEADER_TITLE: str = "Kanpan - all-seeing agent tracker - 看 πᾶν"

# Slots in the footer belt: the status text (borrowed by the search prompt) and the key legend.
_FOOTER_STATUS_SLOT: int = 0
_FOOTER_LEGEND_SLOT: int = 1
# Row the belt occupies in the footer Pile, under a blank one.
_FOOTER_BELT_ROW: int = 1
# Belt legend shown while the search prompt is open, in place of the board's keys.
_SEARCH_LEGEND: tuple[tuple[str, str], ...] = (("↑↓", "next"), ("enter", "select"), ("esc", "cancel"))

TERMINAL_TITLE: str = "kanpan"
# XTWINOPS title stack: push the previous title on entry, pop it back on exit
# (terminals without title-stack support ignore these).
_TITLE_STACK_PUSH: str = "\x1b[22;0t"
_TITLE_STACK_POP: str = "\x1b[23;0t"
# Within a legend binding (`enter: attach`), NBSP keeps the key and its
# description on one line, so a narrow footer wraps between bindings only.
_LEGEND_NBSP: str = "\u00a0"
# Space between bindings in the footer legend belt.
_LEGEND_SEPARATOR: str = "  "
# Space between bindings in a footer-slot panel's key hint (`enter: apply · esc: cancel`).
_PANEL_HINT_SEPARATOR: str = " · "
# Gap the header keeps on either side of its status text, so a status that fits
# is separated from the title as well as from the right edge.
_HEADER_STATUS_PAD: str = "  "
# Blank kept at the belt's right edge so the legend does not touch the terminal border.
_LEGEND_TRAILING_PAD: str = "  "
# Gap the footer Columns puts between the status slot and the legend.
_FOOTER_DIVIDECHARS: int = 1
# Room the belt keeps for the query while the search prompt is open.
_SEARCH_QUERY_MIN_COLS: int = 24
# Stand-in width used before a screen exists to measure against, so nothing is dropped.
_LEGEND_UNMEASURED_COLS: int = 10_000
# The refresh stamp shows the fetch duration for this long, then ages to `5m ago`.
_STAMP_JUST_NOW_SECONDS: float = 10.0
# How often the relative refresh stamp re-renders while the board is idle.
_STAMP_TICK_SECONDS: float = 10.0

PALETTE = [
    ("header", "white", "dark blue"),
    # The footer is a full-width blue belt separating the board from the terminal.
    ("footer", "white", "dark blue"),
    # Keys inside the footer legend, visually distinct from their descriptions.
    ("footer_key", "yellow,bold", "dark blue"),
    # Key accent for default-background legends (the peek hint and the ? overlay).
    ("help_key", "dark cyan,bold", ""),
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
    ("section_draft", "light blue", ""),
    ("section_prs_failed", "light red", ""),
    # CI checks (only failing and pending get color; passing is default)
    ("check_failing", "light red", ""),
    ("check_failing_focus", "light red,standout", ""),
    ("check_pending", "yellow", ""),
    ("check_pending_focus", "yellow,standout", ""),
    ("muted", "dark gray", ""),
    # Plain standout (not dark gray + standout): the focused row must be one
    # continuous highlight band; inverting dim gray would punch dark holes in it.
    ("muted_focus", "standout", ""),
    ("section_muted", "dark gray", ""),
    # Stale: applied per-cell when a field's `created` is older than
    # `staleness_threshold_seconds`. Same color as muted so the visual
    # language is "this is de-emphasized."
    ("stale", "dark gray", ""),
    ("stale_focus", "standout", ""),
    ("error_text", "light red", ""),
    ("notification", "white", "dark magenta"),
    # Peek panel
    ("peek_hint", "dark gray", ""),
    # Your own messages/replies, marked with `›`.
    ("peek_user", "dark blue", ""),
    # Prompted-command input: its caption and its key hint. Styled independently of
    # the peek panel even though both own the footer slot.
    ("prompt_caption", "yellow,bold", ""),
    ("prompt_hint", "dark gray", ""),
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

# Section heading prefix/suffix text lives in data_types (SECTION_PREFIX /
# SECTION_SUFFIX). Only the urwid color attribute is display-specific and stays here.
_SECTION_ATTR: dict[BoardSection, str] = {
    BoardSection.PR_MERGED: "section_done",
    BoardSection.PR_CLOSED: "section_cancelled",
    BoardSection.PR_BEING_REVIEWED: "section_in_review",
    BoardSection.PR_DRAFT: "section_draft",
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
_BUILTIN_COMMAND_KEY_SEARCH = "/"

_BUILTIN_COMMANDS: dict[str, ActionBuiltinCommand | MarkableBuiltinCommand] = {
    _BUILTIN_COMMAND_KEY_SEARCH: ActionBuiltinCommand(role=ActionBuiltinRole.SEARCH, name="search"),
    _BUILTIN_COMMAND_KEY_REFRESH: ActionBuiltinCommand(role=ActionBuiltinRole.REFRESH, name="refresh"),
    _BUILTIN_COMMAND_KEY_PUSH: MarkableBuiltinCommand(
        role=MarkableBuiltinRole.PUSH, name="mark push", markable="yellow"
    ),
    _BUILTIN_COMMAND_KEY_DELETE: MarkableBuiltinCommand(
        role=MarkableBuiltinRole.DELETE, name="mark delete", markable="light red"
    ),
    _BUILTIN_COMMAND_KEY_MUTE: ActionBuiltinCommand(role=ActionBuiltinRole.MUTE, name="mute"),
    _BUILTIN_COMMAND_KEY_UNMARK: ActionBuiltinCommand(role=ActionBuiltinRole.UNMARK, name="unmark"),
    _BUILTIN_COMMAND_KEY_EXECUTE: ActionBuiltinCommand(role=ActionBuiltinRole.EXECUTE, name="execute"),
}

_DEFAULT_MARK_COLOR = "light cyan"

# All attributes that can appear in agent lines and need focus variants
_AGENT_LINE_ATTRS = (
    "state_running",
    "state_attention",
    "check_failing",
    "check_pending",
    "muted",
    "stale",
)

# Column layout configuration
_COL_DIVIDER_CHARS = 2


def _mark_color(cmd: KanpanCommand) -> str | None:
    """Return the mark indicator color if ``cmd`` is markable, else ``None``.

    ``ActionBuiltinCommand`` is never markable. ``MarkableBuiltinCommand``
    always carries a color string. ``CustomCommand.markable`` is
    ``bool | str``: ``False`` means not markable, ``True`` means markable
    with the default color, a ``str`` means that explicit color.
    """
    if isinstance(cmd, ActionBuiltinCommand):
        return None
    if isinstance(cmd, MarkableBuiltinCommand):
        return cmd.markable
    match cmd.markable:
        case str() as color:
            return color
        case bool() as is_markable:
            return _DEFAULT_MARK_COLOR if is_markable else None
        case _:
            assert_never(cmd.markable)


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


class _FitOrHideText(Text):
    """Text that renders blank when the width it is given cannot hold it whole.

    Right-aligned clipping drops leading characters, so a status too wide for its
    column would otherwise read as a fragment of itself.
    """

    def render(self, size: tuple[int] | tuple[()], focus: bool = False) -> Any:
        if size and size[0] < self.pack()[0]:
            return Text("").render(size, focus)
        return super().render(size, focus)


class _SelectableRow(Columns):
    """A Columns widget that is selectable, allowing it to receive focus.

    `name_cell` is the row's name column widget, so a mark can be redrawn into
    it without rebuilding the row. It is None when the board shows no name
    column, or when that column is not a single piece of text.
    """

    name_cell: Text | None = None

    def selectable(self) -> bool:
        return True

    def keypress(self, size: tuple[()] | tuple[int] | tuple[int, int], key: str) -> str | None:
        """Pass all keys through (no keys are handled by this widget)."""
        return key


class _OpenPrompt(FrozenModel):
    """An open one-line input over the footer, and what to do with the text it collects.

    Must stay above ``_KanpanState``: ``tui`` has no ``from __future__ import
    annotations``, so pydantic resolves the referencing field's annotation at
    class-definition time.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    edit: ReadlineEdit
    # Everything the action needs is bound into the handler when the prompt opens, so a
    # refresh landing mid-typing cannot change what the answer applies to.
    on_submit: Callable[[str], None]


class _KanpanState(MutableModel):
    """Mutable state for the kanpan TUI."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    mngr_ctx: MngrContext
    snapshot: BoardSnapshot | None = None
    frame: Any  # urwid Frame widget
    footer_left_text: Any  # urwid Text widget (left side of footer)
    footer_left_attr: Any  # urwid AttrMap wrapping footer_left_text
    footer_right: Any  # urwid Text widget (right side of footer)
    # urwid Text widget carrying the header's rendered status text.
    header_status_text: Any
    loop: Any = None  # urwid MainLoop, set after construction
    spinner_index: int = 0
    refresh_future: Future[FetchResult] | None = None
    # In-memory cache of fields from previous refresh cycle, keyed by agent id
    cached_fields: dict[AgentId, dict[str, FieldValue]] = {}
    executor: ThreadPoolExecutor | None = None
    # The in-flight periodic local refresh, and the worker that serves it. An in-flight
    # fetch cannot be cancelled, so a tick that runs most of the time would hold
    # `refresh_future` almost always and the periodic full refresh, which skips whenever
    # that field is taken, would stop happening. Hence a worker and a field of its own.
    local_refresh_future: Future[FetchResult] | None = None
    local_refresh_executor: ThreadPoolExecutor | None = None
    # Dired-style marks: command key an agent is flagged with, keyed by the
    # agent *instance* (``<agent_id>@<host_id>``) so two agents that share a
    # name -- or even an id (agent ids are only unique per host) -- on
    # different hosts mark and act independently.
    marks: dict[AgentInstanceKey, str] = {}
    # Active batch execution state
    executing: bool = False
    # Failures from the most recent batch execution, rendered at the bottom of
    # the board (like fetch errors) until the next execution clears them.
    execute_errors: tuple[str, ...] = ()
    # Maps list walker index -> AgentBoardEntry for selectable agent entries
    index_to_entry: dict[int, AgentBoardEntry] = {}
    list_walker: Any = None  # SimpleFocusListWalker, set during display build
    # Instance of the agent that was focused before refresh (for focus persistence). Keyed
    # by instance, not name or bare id, so a refresh restores focus to the exact agent even
    # when a name or id is shared across hosts.
    focused_instance_key: AgentInstanceKey | None = None
    # Steady-state footer left text (shown when nothing higher-priority is active)
    steady_footer_text: str = "  Loading..."
    # --- Footer rendering (single-owner model) ---
    # The footer-left widget has exactly one writer (`_render_footer`), which picks
    # what to show from the fields below by priority. This prevents the flicker that
    # arose when several independent alarm loops (refresh spinner, batch action,
    # custom command) each wrote the shared widget on overlapping ticks.
    # Transient notification text; overrides everything while set.
    transient_message: str | None = None
    # Alarm handle that clears `transient_message` (None if none pending).
    transient_alarm: Any = None
    # Base text (without the spinner glyph) of an in-progress user action -- batch
    # execution or a custom command. Takes priority over the background refresh.
    action_label: str | None = None
    # Handle for the single animation tick that advances the spinner (None if idle).
    animation_alarm: Any = None
    # All commands (builtins merged with user config), keyed by trigger key
    commands: dict[str, KanpanCommand] = {}
    # Monotonic timestamp of the refresh the stamp reports the age of. 0.0 until one lands.
    last_successful_refresh_time: float = 0.0
    # Monotonic timestamp of the last full refresh attempt, succeeded or not (for cooldown logic).
    last_refresh_attempt_time: float = 0.0
    # Fetch duration of the last full refresh, shown briefly in the stamp.
    last_fetch_seconds: float | None = None
    # Whether the current in-flight refresh is local-only (no GitHub API)
    refresh_is_local_only: bool = False
    # Whether a local refresh was asked for while one was already in flight, and so runs
    # once that one finishes. Only one is held: the board has a single state to catch up to.
    is_local_refresh_pending: bool = False
    # Whether a periodic full refresh came due while a local-only one held the slot, and so
    # runs once that one finishes.
    is_full_refresh_pending: bool = False
    # Handle for the pending deferred refresh alarm (None if no alarm is pending)
    deferred_refresh_alarm: Any = None
    # Monotonic time the deferred refresh is scheduled to fire
    deferred_refresh_fire_at: float = 0.0
    # Cooldown durations (loaded from plugin config)
    refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS
    # Seconds between periodic local refreshes; 0 runs them only on an action.
    local_refresh_interval_seconds: float = DEFAULT_LOCAL_REFRESH_INTERVAL_SECONDS
    retry_cooldown_seconds: float = 60.0
    staleness_threshold_seconds: float = DEFAULT_STALENESS_THRESHOLD_SECONDS
    # When true, Left on an empty reply closes the peek panel (Agent-View back gesture).
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
    # --- Peek panel (None name => panel closed) ---
    # Name of the agent currently shown in the peek panel (for the panel title and focus).
    peek_agent_name: AgentName | None = None
    # Instance key of that agent (a host-scoped mngr address), used to resolve the
    # transcript fetch and reply send unambiguously.
    peek_instance_key: AgentInstanceKey | None = None
    # Original frame footer (keybinding bar), restored when the peek panel or the
    # prompt closes. Shared because the two are mutually exclusive: the peek gate
    # precedes command dispatch, and the prompt gate precedes the peek key.
    saved_footer: Any = None
    # Widgets owned by the open panel (set while peek_agent_name is not None).
    peek_box: Any = None
    peek_body_text: Any = None
    peek_input: Any = None
    # Last clean `mngr transcript` stdout (see _run_transcript) shown in the panel.
    peek_transcript: str = ""
    # Replies sent but not yet echoed back by the transcript, shown optimistically.
    peek_pending_replies: list[str] = []
    # In-flight transcript read for the peeked agent, polled by the peek alarm.
    peek_capture_future: Future[subprocess.CompletedProcess[str]] | None = None
    # Handle for the pending live-refresh alarm (None if none scheduled).
    peek_alarm: Any = None
    # Most recent `mngr message` reply send; each send is watched by its own poll alarm.
    peek_reply_future: Future[subprocess.CompletedProcess[str]] | None = None
    # Failure detail of the most recent reply send, rendered in the panel body ("" if none).
    peek_reply_error: str = ""
    # Executor for peek transcript reads. Kept separate from the shared `executor`
    # so a read cannot freeze a board refresh, and from the reply executor so a slow
    # reply does not stall the live body refresh.
    peek_executor: ThreadPoolExecutor | None = None
    # Single-worker executor for replies, so several queued replies reach the agent
    # in submission order and their `mngr message` pastes cannot interleave.
    peek_reply_executor: ThreadPoolExecutor | None = None
    # Compiled `header_status` template and its counts (None when unconfigured).
    header_status: HeaderStatus | None = None
    # Every key binding shown by the `?` overlay, in display order.
    legend_bindings: list[tuple[str, str]] = []
    # The board's own belt legend, restored when a prompt stops borrowing the belt.
    footer_legend: list[tuple[str, str]] = []
    # Legend the belt is currently showing, re-fitted whenever its room changes.
    active_legend: list[tuple[str, str]] = []
    # Text the belt carries ahead of that legend, and takes room from it: the match counter.
    active_legend_prefix: str = ""
    # The `?` overlay widget while it is open (None otherwise).
    help_overlay: Any = None
    # --- Footer prompt (None => no prompt open) ---
    # The open one-line input and the handler that answers it.
    open_prompt: _OpenPrompt | None = None
    # Single-worker executor for prompted commands, kept off the shared `executor` so a
    # write against an unresponsive host cannot stall a board refresh for the whole timeout.
    prompt_executor: ThreadPoolExecutor | None = None
    # A command's outcome, held until the refresh it asked for has repainted the board, so
    # the message and the rows it describes appear together instead of a second apart.
    pending_completion_message: str | None = None
    # Executor for marked operations. Separate from the shared `executor` so a long batch
    # cannot hold up a board refresh, and multi-worker so independent agents overlap.
    batch_executor: ThreadPoolExecutor | None = None
    # How many marked operations may be in flight at once (from plugin config).
    batch_concurrency: int = 4
    # --- Search prompt (None input => closed) ---
    # The Frame's footer: a blank row above the belt. Focused while the prompt is open,
    # since Frame routes keys only to the part it has focus on.
    footer_pile: Any = None
    # Edit widget holding the query while the prompt is open.
    search_input: Any = None
    # The footer belt's Columns, whose status slot the prompt borrows.
    footer_columns: Any = None
    # Rows matching the current query, best match first.
    search_matches: tuple[AgentName, ...] = ()
    # Index into `search_matches` of the row currently focused.
    search_index: int = 0
    # Row focused before the prompt opened, restored when the search is cancelled.
    pre_search_focus: AgentName | None = None


class _KanpanInputHandler(MutableModel):
    """Callable input handler for the kanpan TUI."""

    state: _KanpanState

    def __call__(self, key: str | tuple[str, int, int, int]) -> bool | None:
        """Handle keyboard input. Returns True if handled, None to pass through."""
        if isinstance(key, tuple):
            return None
        # While the help overlay is open it owns the keyboard.
        if self.state.help_overlay is not None:
            if key in ("?", "esc", "q"):
                _close_help(self.state)
            return True
        # While the peek panel is open it owns the keyboard; printable keys have
        # already been consumed by its reply Edit before reaching here.
        if self.state.peek_agent_name is not None:
            return _handle_peek_key(self.state, key)
        # While a command prompt is open it owns the keyboard; printable keys have already
        # been consumed by its input Edit before reaching here. This gate must stay above
        # the branches below that claim the keys the Edit does refuse: `ctrl c` would quit
        # kanpan, `enter` would attach to the agent, and `up` would clear board focus.
        if self.state.open_prompt is not None:
            return _handle_prompt_key(self.state, key)
        # Likewise for the search prompt, so `q` and the command keys type into
        # the query instead of quitting or acting on the focused agent.
        if self.state.search_input is not None:
            return _handle_search_key(self.state, key)
        if key == "?":
            _open_help(self.state)
            return True
        if key == " ":
            _toggle_peek(self.state)
            return True
        if key in ("q", "ctrl c"):
            raise ExitMainLoop()
        if key == "U":
            _unmark_all(self.state)
            return True
        cmd = self.state.commands.get(key)
        if cmd is not None:
            _dispatch_command(self.state, key, cmd)
            return True
        if key == "enter":
            _attach_to_focused_agent(self.state)
            return True
        if key == "up":
            if _is_focus_on_first_selectable(self.state):
                _clear_focus(self.state)
                return True
            return None
        if key in ("down", "page up", "page down", "home", "end"):
            return None
        return True


class _BoardFrame(Frame):
    """Frame that ends an open search prompt when a left press lands on the board.

    urwid moves the Frame's focus to whichever part a left press lands in, which
    would leave the prompt open but keyboardless, its match still highlighted
    beside the row that was clicked. Ending the search from the mouse event keeps
    it in step with the keys around it: urwid hands a whole read of input over at
    once, so a key typed just before the click arrives in the same batch and must
    still reach the query.
    """

    kanpan_state: Any = None

    def mouse_event(
        self, size: tuple[int, int], event: str, button: int, col: int, row: int, focus: bool
    ) -> bool | None:
        state = self.kanpan_state
        if state is not None and state.search_input is not None and is_mouse_press(event) and button == 1:
            _maxcol, maxrow = size
            (header_rows, footer_rows), _originals = self.frame_top_bottom(size, focus)
            if header_rows <= row < maxrow - footer_rows:
                _close_search(state, is_cancelled=False)
        return super().mouse_event(size, event, button, col, row, focus)


def _is_modal_surface_open(state: _KanpanState) -> bool:
    """Whether the prompt, the peek panel, or the `?` overlay currently owns the keyboard.

    The search prompt is deliberately absent: it answers a board click by ending the
    search on the row clicked (see ``_BoardFrame``), which withholding the event would
    silently undo.
    """
    return state.open_prompt is not None or state.peek_agent_name is not None or state.help_overlay is not None


class _KanpanInputFilter(MutableModel):
    """Input the board sees before the widget tree does.

    A resize refits the footer legend. And while a modal surface owns the keyboard the
    board is shown no mouse events at all: urwid routes those by position rather than
    focus, so a click would otherwise reach the board's ListBox and move the selection
    out from under an open panel -- ListBox.mouse_event changes focus even while
    reporting the event unhandled. This is the only place that can stop it;
    ``unhandled_input`` runs after the widget tree has already acted.
    """

    state: _KanpanState

    # Elements are Any because urwid types this list as list[str] while a mouse event
    # arrives in it as a ("mouse press", button, col, row) tuple -- the thing being dropped.
    def __call__(self, keys: list[Any], raw: list[int]) -> list[Any]:
        if "window resize" in keys:
            _render_footer(self.state)
        if not _is_modal_surface_open(self.state):
            return list(keys)
        return [key for key in keys if not isinstance(key, tuple)]


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
    state.focused_instance_key = None
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


def _run_destroy(agent_addresses: list[str]) -> subprocess.CompletedProcess[str]:  # pragma: no cover
    """Run mngr destroy in a subprocess. Called from a background thread.

    Addresses are host-scoped instance keys (``<agent_id>@<host_id>``) so a
    marked row can never destroy a same-id agent on another host.
    """
    return subprocess.run(
        ["mngr", "destroy", *agent_addresses, "--force"],
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
        name_markup = _flatten_markup_to_attr(name_markup, "muted")
    attr_map_widget = state.list_walker[walker_idx]
    row: _SelectableRow = attr_map_widget.original_widget
    if row.name_cell is None:
        return
    row.name_cell.set_text(name_markup)


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

    existing = state.marks.get(entry.instance_key)
    if existing == key:
        del state.marks[entry.instance_key]
        new_mark = None
    else:
        state.marks[entry.instance_key] = key
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
    if entry.instance_key in state.marks:
        del state.marks[entry.instance_key]
        _update_row_mark(state, focus_idx, None)
        _update_mark_count_footer(state)


def _unmark_all(state: _KanpanState) -> None:
    """Remove all marks."""
    if not state.marks:
        return
    marked_keys = set(state.marks.keys())
    state.marks.clear()
    for idx, entry in state.index_to_entry.items():
        if entry.instance_key in marked_keys:
            _update_row_mark(state, idx, None)
    _update_mark_count_footer(state)


def _prune_orphaned_marks(state: _KanpanState) -> None:
    """Remove marks for agent instances that are no longer in the current snapshot."""
    if state.snapshot is None or not state.marks:
        return
    current_keys = {e.instance_key for e in state.snapshot.entries}
    orphaned = [instance_key for instance_key in state.marks if instance_key not in current_keys]
    for instance_key in orphaned:
        del state.marks[instance_key]
    if orphaned:
        _update_mark_count_footer(state)


def _update_mark_count_footer(state: _KanpanState) -> None:
    """Re-render the footer after the set of marked agents changed."""
    _render_footer(state)


class _CollectBatchInput(FrozenModel):
    """Prompt handler that banks one marked command's answer and moves the batch along."""

    state: _KanpanState
    key: str
    remaining_keys: tuple[str, ...]
    collected: dict[str, str]

    def __call__(self, input_text: str) -> None:
        _collect_batch_input(self.state, self.remaining_keys, {**self.collected, self.key: input_text})


def _prompted_mark_keys(state: _KanpanState) -> tuple[str, ...]:
    """Keys of the marked commands that ask for a value, in the order they were marked."""
    keys: list[str] = []
    for mark_key in state.marks.values():
        cmd = state.commands.get(mark_key)
        if isinstance(cmd, CustomCommand) and cmd.prompt and mark_key not in keys:
            keys.append(mark_key)
    return tuple(keys)


def _collect_batch_input(state: _KanpanState, remaining_keys: tuple[str, ...], collected: Mapping[str, str]) -> None:
    """Ask for the next prompted command's value, or start the batch once every answer is in.

    One prompt per marked command rather than per agent: the answer applies to all of
    that command's marks. Cancelling a prompt runs nothing at all and leaves the marks,
    so a batch is never half-applied on a change of mind.
    """
    if not remaining_keys:
        _start_batch_execution(state, collected)
        return
    key, rest = remaining_keys[0], remaining_keys[1:]
    cmd = state.commands.get(key)
    if not isinstance(cmd, CustomCommand):
        _collect_batch_input(state, rest, collected)
        return
    marked_count = sum(1 for marked_key in state.marks.values() if marked_key == key)
    _open_prompt(
        state,
        title=f" {cmd.name} · {marked_count} marked ",
        caption=cmd.prompt,
        on_submit=_CollectBatchInput(state=state, key=key, remaining_keys=rest, collected=dict(collected)),
    )


def _execute_marks(state: _KanpanState) -> None:
    """Execute all pending marks, asking first for any prompted command's value."""
    # An open prompt means a collection round is already under way; a second `x` would
    # stack a prompt over it and strand the first.
    if not state.marks or state.executing or state.open_prompt is not None:
        return
    _collect_batch_input(state, _prompted_mark_keys(state), {})


class _BatchWorkItem(FrozenModel):
    instance_key: AgentInstanceKey
    name: AgentName
    key: str
    cmd: KanpanCommand
    entry: AgentBoardEntry | None
    # Delete builtin only: every marked agent instance, batched into one `mngr destroy`.
    # Parallel by index -- instance keys (host-scoped addresses) resolve the destroy and
    # clear the marks, names label the per-agent results.
    batch_instance_keys: tuple[AgentInstanceKey, ...] = ()
    batch_names: tuple[AgentName, ...] = ()
    # Answer collected once for this item's command, shared by every agent marked with it.
    input_text: str = ""


class _BatchItemResult(FrozenModel):
    """Outcome of executing one marked operation (or one agent within a batch)."""

    label: str
    is_success: bool
    # For failures, the captured stderr or exception text shown to the user.
    detail: str = ""


@pure
def _batch_item_label(item: _BatchWorkItem) -> str:
    """Format a human-readable label for a batch work item."""
    if item.batch_instance_keys:
        return f"{item.cmd.name} {len(item.batch_instance_keys)} agent(s)"
    return f"{item.cmd.name} {item.name}"


def _run_shell_command_sync(
    command: str, agent_name: str, input_text: str, instance_key: AgentInstanceKey
) -> subprocess.CompletedProcess[str]:
    """Run a custom command's shell string for one agent. Called from a background thread.

    A prompted command's typed text arrives as ``MNGR_INPUT`` rather than interpolated
    into the command string, keeping it out of the shell's parse phase.

    ``MNGR_AGENT_ID`` and ``MNGR_AGENT_NAME`` are each unique only per host (an
    agent id can exist on multiple hosts, e.g. mid-migration); ``MNGR_HOST_ID``
    scopes them, and ``"$MNGR_AGENT_ID@$MNGR_HOST_ID"`` is a full mngr address
    for the exact instance. All are always set, overriding any value inherited
    from the board's own environment.
    """
    env = {
        **os.environ,
        "MNGR_AGENT_NAME": agent_name,
        "MNGR_AGENT_ID": str(instance_key.agent_id),
        "MNGR_HOST_ID": str(instance_key.host_id),
        "MNGR_INPUT": input_text,
    }
    return subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _start_batch_execution(state: _KanpanState, input_text_by_key: Mapping[str, str]) -> None:
    """Begin executing all marked operations, with any prompted answers in hand."""
    state.executing = True
    # Clear failures from any previous run so a fresh attempt starts clean.
    state.execute_errors = ()

    entries_by_instance: dict[AgentInstanceKey, AgentBoardEntry] = {}
    if state.snapshot is not None:
        entries_by_instance = {e.instance_key: e for e in state.snapshot.entries}

    delete_instance_keys: list[AgentInstanceKey] = []
    delete_names: list[AgentName] = []
    individual_work: list[_BatchWorkItem] = []
    for instance_key, mark_key in state.marks.items():
        cmd = state.commands.get(mark_key)
        if cmd is None:
            continue
        # A mark whose agent is no longer on the board is not actionable; skip it (pruning
        # normally removes such marks already). This also guarantees a name for every item.
        entry = entries_by_instance.get(instance_key)
        if entry is None:
            continue
        # Only the builtin delete batches all marked agents into one `mngr
        # destroy` call. A user-defined override of "d" (or any other key)
        # runs per-agent via the individual-work path.
        if isinstance(cmd, MarkableBuiltinCommand) and cmd.role == MarkableBuiltinRole.DELETE:
            delete_instance_keys.append(instance_key)
            delete_names.append(entry.name)
        else:
            individual_work.append(
                _BatchWorkItem(
                    instance_key=instance_key,
                    name=entry.name,
                    key=mark_key,
                    cmd=cmd,
                    entry=entry,
                    input_text=input_text_by_key.get(mark_key, ""),
                )
            )

    work: list[_BatchWorkItem] = []
    if delete_instance_keys:
        delete_cmd = state.commands.get(_BUILTIN_COMMAND_KEY_DELETE)
        if delete_cmd is not None:
            work.append(
                _BatchWorkItem(
                    instance_key=delete_instance_keys[0],
                    name=delete_names[0],
                    key=_BUILTIN_COMMAND_KEY_DELETE,
                    cmd=delete_cmd,
                    entry=entries_by_instance.get(delete_instance_keys[0]),
                    batch_instance_keys=tuple(delete_instance_keys),
                    batch_names=tuple(delete_names),
                )
            )
    work.extend(individual_work)

    _execute_batch(state, work)


def _submit_batch_item(
    executor: ThreadPoolExecutor, item: _BatchWorkItem
) -> Future[subprocess.CompletedProcess[str]] | None:
    """Submit a single batch work item to the executor."""
    match item.cmd:
        case MarkableBuiltinCommand():
            match item.cmd.role:
                case MarkableBuiltinRole.DELETE:
                    keys = item.batch_instance_keys if item.batch_instance_keys else (item.instance_key,)
                    return executor.submit(_run_destroy, [str(key) for key in keys])
                case MarkableBuiltinRole.PUSH:
                    if item.entry is None or item.entry.work_dir is None:
                        return None
                    return executor.submit(_run_git_push, str(item.entry.work_dir))
                case _:
                    assert_never(item.cmd.role)
        case ActionBuiltinCommand():
            # Non-markable builtins never reach batch dispatch.
            return None
        case CustomCommand():
            if item.cmd.command:
                return executor.submit(
                    _run_shell_command_sync, item.cmd.command, str(item.name), item.input_text, item.instance_key
                )
            return None
        case _:
            assert_never(item.cmd)


def _record_batch_result(
    state: _KanpanState,
    item: _BatchWorkItem,
    future: Future[subprocess.CompletedProcess[str]],
    results: list[_BatchItemResult],
) -> None:
    """Bank one finished operation's outcome, clearing its mark only if it succeeded."""
    label = _batch_item_label(item)
    try:
        result = future.result()
        if result.returncode == 0:
            # The builtin delete runs every marked agent through one `mngr destroy`, so
            # its single result stands for all of them. Clear marks by instance, label by name.
            batch_instance_keys = item.batch_instance_keys or (item.instance_key,)
            batch_names = item.batch_names or (item.name,)
            for instance_key, name in zip(batch_instance_keys, batch_names, strict=True):
                results.append(_BatchItemResult(label=f"{item.cmd.name} {name}", is_success=True))
                state.marks.pop(instance_key, None)
        else:
            detail = result.stderr.strip() or f"exited with code {result.returncode}"
            results.append(_BatchItemResult(label=label, is_success=False, detail=detail))
    except subprocess.TimeoutExpired as e:
        results.append(_BatchItemResult(label=label, is_success=False, detail=f"timed out after {e.timeout:g}s"))
    except Exception as e:
        results.append(_BatchItemResult(label=label, is_success=False, detail=str(e)))


def _execute_batch(state: _KanpanState, work: list[_BatchWorkItem]) -> None:
    """Start every marked operation, then watch them all finish.

    Marked agents are independent, so the work goes out together and the executor's
    worker count is what limits overlap.
    """
    executor = _ensure_batch_executor(state)
    results: list[_BatchItemResult] = []
    in_flight: list[tuple[_BatchWorkItem, Future[subprocess.CompletedProcess[str]]]] = []
    for item in work:
        future = _submit_batch_item(executor, item)
        if future is None:
            results.append(
                _BatchItemResult(label=_batch_item_label(item), is_success=False, detail="skipped (not executable)")
            )
        else:
            in_flight.append((item, future))

    if not in_flight:
        _finish_batch_execution(state, results)
        return

    _render_batch_progress(state, done_count=len(work) - len(in_flight), total=len(work))
    _ensure_animation_running(state)
    if state.loop is not None:
        state.loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_batch_poll, (state, in_flight, results, len(work)))


def _render_batch_progress(state: _KanpanState, done_count: int, total: int) -> None:
    """Show how far through the batch we are; with several in flight no single item stands for it."""
    state.action_label = f"  Executing {done_count}/{total}"
    _render_footer(state)


def _on_batch_poll(
    loop: MainLoop,
    data: tuple[
        _KanpanState,
        list[tuple[_BatchWorkItem, Future[subprocess.CompletedProcess[str]]]],
        list[_BatchItemResult],
        int,
    ],
) -> None:
    """Collect whichever operations have finished, and keep watching the rest."""
    state, in_flight, results, total = data
    still_running: list[tuple[_BatchWorkItem, Future[subprocess.CompletedProcess[str]]]] = []
    for item, future in in_flight:
        if future.done():
            _record_batch_result(state, item, future, results)
        else:
            still_running.append((item, future))

    if not still_running:
        _finish_batch_execution(state, results)
        return

    _render_batch_progress(state, done_count=total - len(still_running), total=total)
    loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_batch_poll, (state, still_running, results, total))


def _finish_batch_execution(state: _KanpanState, results: list[_BatchItemResult]) -> None:
    """Complete batch execution and show summary."""
    state.executing = False
    state.action_label = None

    ok_count = sum(1 for r in results if r.is_success)
    failures = [r for r in results if not r.is_success]

    # Persist failure detail so it renders at the bottom of the board (the same
    # place fetch/GitHub errors appear) until the next execution clears it. The
    # transient footer message alone is too easy to miss.
    state.execute_errors = tuple(f"{r.label}: {r.detail}" if r.detail else r.label for r in failures)

    if not failures:
        summary = f"  Executed {ok_count} operation(s) successfully"
    else:
        summary = f"  Executed: {ok_count} ok, {len(failures)} failed"

    _refresh_display(state)

    # Local-only refresh to immediately show updated state, with the summary held back
    # until it lands so the count and the rows it counts arrive together.
    if state.loop is not None:
        _report_after_refresh(state, summary, state.loop)
    else:
        _show_transient_message(state, summary)


_LOCALLY_WRITTEN_FIELDS: Final[tuple[str, ...]] = (FIELD_MUTED,)


@pure
def _read_muted(fields: Mapping[str, FieldValue]) -> bool:
    """Whether `fields` marks the agent as muted."""
    field = fields.get(FIELD_MUTED)
    if field is None:
        return False
    if not isinstance(field, BoolField):
        raise KanpanFieldTypeError(f"Expected BoolField for '{FIELD_MUTED}', got {type(field).__name__}")
    return field.value


@pure
def _with_fields(entry: AgentBoardEntry, fields: dict[str, FieldValue]) -> AgentBoardEntry:
    """Return an updated AgentBoardEntry carrying `fields`.

    Rebuilds cells, section and is_muted from them, so everything the row derives from its
    fields stays in step with them.
    """
    ref = entry.field_ref()
    return entry.model_copy_update(
        to_update(ref.is_muted, _read_muted(fields)),
        to_update(ref.fields, fields),
        to_update(ref.cells, {key: field.display() for key, field in fields.items()}),
        to_update(ref.section, compute_section(fields)),
    )


def _apply_mute_to_entry(entry: AgentBoardEntry, is_muted: bool) -> AgentBoardEntry:
    """Return an updated AgentBoardEntry with the mute state applied, as of now."""
    muted = BoolField(value=is_muted, created=now_utc())
    return _with_fields(entry, {**entry.fields, FIELD_MUTED: muted})


def _update_snapshot_mute(state: _KanpanState, instance_key: AgentInstanceKey, is_muted: bool) -> None:
    """Update the snapshot in-place by setting the mute state on this agent instance."""
    if state.snapshot is None:
        return
    new_entries = tuple(
        _apply_mute_to_entry(entry, is_muted) if entry.instance_key == instance_key else entry
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

    instance_key = entry.instance_key
    agent_name = entry.name
    new_muted = not entry.is_muted

    # Optimistic UI update. Stamped now, so a fetch that read this agent before the keypress
    # loses to it in `_prefer_later_read` however long it takes to land.
    _update_snapshot_mute(state, instance_key, new_muted)
    _refresh_display(state)
    action = "Muted" if new_muted else "Unmuted"
    _show_transient_message(state, f"  {action} {agent_name}")

    # Persist in background
    def _do_mute() -> None:
        set_agent_mute(state.mngr_ctx, instance_key.agent_id, instance_key.host_id, entry.provider_name, new_muted)

    future = state.executor.submit(_do_mute)
    if state.loop is not None:
        state.loop.set_alarm_in(
            SPINNER_INTERVAL_SECONDS, _on_mute_persist_poll, (state, future, instance_key, agent_name, new_muted)
        )


def _on_mute_persist_poll(
    loop: MainLoop, data: tuple[_KanpanState, Future[None], AgentInstanceKey, AgentName, bool]
) -> None:
    """Poll for mute persist completion. Revert UI on failure."""
    state, future, instance_key, agent_name, expected_muted = data
    if future.done():
        try:
            future.result()
        except Exception as e:
            # Revert the optimistic update
            _update_snapshot_mute(state, instance_key, not expected_muted)
            _refresh_display(state)
            _show_transient_message(state, f"  Failed to persist mute for {agent_name}: {e}")
    else:
        loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_mute_persist_poll, data)


def _attach_to_focused_agent(state: _KanpanState) -> None:  # pragma: no cover
    """Suspend the board and attach to the focused agent's session, restoring on return.

    Runs ``mngr connect`` as a child process that takes over the terminal (locally it
    execs ``tmux attach``), so the board's MainLoop is stopped for the duration and
    restarted when the user detaches. Connecting in-process is not an option: the
    connect API replaces the running process via ``execvpe``, which would tear down
    the board itself.
    """
    entry = _get_focused_entry(state)
    if entry is None:
        return
    loop = state.loop
    if loop is None:
        return

    # Attaching is an explicit full-screen takeover, so force it through even when
    # kanpan itself is running inside tmux: `mngr connect` otherwise refuses a
    # nested attach unless is_nested_tmux_allowed is set. Dropping TMUX from the
    # child env is a no-op when kanpan runs in a plain terminal.
    attach_env = {key: value for key, value in os.environ.items() if key not in ("TMUX", "TMUX_PANE")}

    loop.stop()
    try:
        # loop.stop() restores the pre-kanpan primary screen (a stale shell prompt);
        # clear it and show a connecting line so the handoff is not a flash of that
        # old command line before the agent's session paints over it.
        write_human_line(f"{CLEAR_SCREEN}  Connecting to {entry.name}...")
        result = subprocess.run(["mngr", "connect", str(entry.instance_key)], env=attach_env)
    finally:
        # The attached session sets its own terminal title; take it back.
        _write_terminal_title(loop.screen, TERMINAL_TITLE)
        loop.start()
        # A terminal resized while the loop was stopped reached no handler, so urwid's cached
        # size predates the attach. Clearing it makes the repaint below measure again.
        loop.screen_size = None
        loop.screen.clear()
        # Force an immediate repaint so the board returns at once instead of waiting for
        # the next refresh, which would leave the detach output on screen.
        loop.draw_screen()

    if result.returncode != 0:
        # The child's own error output went to the terminal the repaint just erased; the
        # exit code is the only signal left (its output is not captured because the
        # connect child needs the real TTY for the tmux takeover).
        _show_transient_message(
            state, f"  Connect to {entry.name} failed (mngr connect exited with code {result.returncode})"
        )
    _request_local_refresh(loop, state)


def _find_entry_by_name(state: _KanpanState, name: AgentName | None) -> AgentBoardEntry | None:
    """Find the board entry with the given name among the currently displayed rows."""
    if name is None:
        return None
    for entry in state.index_to_entry.values():
        if entry.name == name:
            return entry
    return None


def _focus_row_by_name(state: _KanpanState, name: AgentName) -> int | None:
    """Move the board's list focus to the row for the named agent; report where it landed."""
    if state.list_walker is None:
        return None
    for idx, entry in state.index_to_entry.items():
        if entry.name == name:
            state.list_walker.set_focus(idx)
            return idx
    return None


def _find_entry_by_instance_key(state: _KanpanState, instance_key: AgentInstanceKey | None) -> AgentBoardEntry | None:
    """Find the board entry for the given agent instance among the currently displayed rows."""
    if instance_key is None:
        return None
    for entry in state.index_to_entry.values():
        if entry.instance_key == instance_key:
            return entry
    return None


def _focus_row_by_instance_key(state: _KanpanState, instance_key: AgentInstanceKey) -> int | None:
    """Move the board's list focus to this agent instance's row; report where it landed."""
    if state.list_walker is None:
        return None
    for idx, entry in state.index_to_entry.items():
        if entry.instance_key == instance_key:
            state.list_walker.set_focus(idx)
            return idx
    return None


def _run_transcript(agent: str) -> subprocess.CompletedProcess[str]:  # pragma: no cover
    """Read the agent's user/agent messages. Called from a background thread.

    The role filter excludes tool and system turns, keeping the readable
    conversation. No ``--tail`` window -- the whole thing is fetched and the panel
    keeps the tail.
    """
    return subprocess.run(
        ["mngr", "transcript", agent, "--role", "user", "--role", "agent"],
        capture_output=True,
        text=True,
        timeout=30,
    )


@pure
def _message_command(agent: str, message: str) -> list[str]:
    """Build the ``mngr message`` argv for a peek reply.

    ``--start`` brings up an offline host and (re)launches a ``STOPPED`` or ``DONE``
    agent, so a reply lands on an agent that is not currently live instead of
    failing with its state. Reviving a ``DONE`` agent tears down its lingering tmux
    session, discarding that pane's content.
    """
    return ["mngr", "message", agent, "--start", "-m", message]


def _run_message(agent: str, message: str) -> subprocess.CompletedProcess[str]:  # pragma: no cover
    """Send a message to the agent. Called from a background thread.

    The timeout is a ceiling on how long one send may hold the single-worker reply
    executor, not a bound on the path: it clears the ~135s a live agent's own waits
    (TUI-ready, paste-visible, submission-confirmation) can take, but a cold start
    can outrun it -- ``--start`` waits on the host coming up, and reviving a DONE
    agent takes a host lock that has no timeout at all. A send cut off here is
    reported as a failure whether or not the message landed.
    """
    return subprocess.run(_message_command(agent, message), capture_output=True, text=True, timeout=180)


def _ensure_peek_executor(state: _KanpanState) -> ThreadPoolExecutor:
    """Return the peek executor, creating it on first use."""
    if state.peek_executor is None:
        state.peek_executor = ThreadPoolExecutor(max_workers=2)
    return state.peek_executor


def _ensure_peek_reply_executor(state: _KanpanState) -> ThreadPoolExecutor:
    """Return the single-worker reply executor, creating it on first use.

    One worker serializes replies so several queued sends reach the agent in the
    order they were typed, without their `mngr message` pastes interleaving.
    """
    if state.peek_reply_executor is None:
        state.peek_reply_executor = ThreadPoolExecutor(max_workers=1)
    return state.peek_reply_executor


@pure
def _last_nonempty_line(text: str) -> str:
    """Return the last non-blank line of `text` (mngr errors append pane dumps)."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


@pure
def _peek_body_lines(transcript: str, pending: list[str]) -> list[str]:
    """Assemble the panel's body lines: the clean conversation plus pending replies.

    Pending replies -- sent but not yet echoed back by the transcript -- are appended as
    ``› `` lines so a reply shows the instant it is sent. Only the trailing
    ``PEEK_BODY_HEIGHT`` lines are kept (a leading ``⋯`` marks older lines were trimmed)
    so a long final message shows its end rather than the agent's scrolled-up screen.
    """
    body = transcript.strip("\n")
    lines = body.split("\n") if body else []
    for reply in pending:
        if lines:
            lines.append("")
        lines.append(f"{PEEK_REPLY_PROMPT}{reply}")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) > PEEK_BODY_HEIGHT:
        lines = lines[-PEEK_BODY_HEIGHT:]
        while lines and not lines[0].strip():
            lines.pop(0)
        lines = ["⋯", *lines]
    return lines


@pure
def _is_transcript_header(line: str) -> bool:
    """True for a ``mngr transcript`` header line, e.g. ``[2026-07-08T02:56:03Z] user:``."""
    stripped = line.rstrip()
    return line.startswith("[") and "] " in line and stripped.endswith(":")


@pure
def _short_header(line: str) -> str:
    """Drop the ISO timestamp from a transcript header, leaving just the role cue.

    ``[2026-07-08T02:56:03Z] assistant:`` -> ``assistant:``. The timestamp is chrome for
    a live peek; the full value is still available via ``mngr transcript`` itself.
    """
    return line.split("] ", 1)[1] if "] " in line else line


@pure
def _peek_body_markup(transcript: str, pending: list[str]) -> list[Any]:
    """Render the panel body as urwid markup: message text prominent, chrome de-emphasized.

    The transcript's own ``[time] role:`` headers are shortened to a dimmed role cue and
    the trim ``⋯`` marker is dimmed too, so the conversation reads first; your
    sent-but-not-yet-echoed replies are accented with the same ``›`` as the reply prompt.
    """
    lines = _peek_body_lines(transcript, pending)
    if not lines:
        return [("peek_hint", "(no messages yet)")]
    markup: list[Any] = []
    for index, line in enumerate(lines):
        if index > 0:
            markup.append("\n")
        if line == "⋯":
            markup.append(("peek_hint", line))
        elif _is_transcript_header(line):
            markup.append(("peek_hint", _short_header(line)))
        elif line.startswith(PEEK_REPLY_PROMPT):
            markup.append(("peek_user", line))
        else:
            markup.append(line)
    return markup


def _make_readline_edit(caption: tuple[str, str], wrap: WrapMode = WrapMode.SPACE) -> ReadlineEdit:
    """A single-line input with readline editing from ``urwid_readline``.

    ``ReadlineEdit`` ships the readline keymap (Ctrl-A/E/W/K/U, Meta-B/F/D, etc.)
    but binds word ops only to Meta+letter and Shift+arrow, not the Option/Ctrl +
    arrow chords many terminals emit; those are added here so word movement
    works however the terminal encodes it. ``enter``, ``esc``, the vertical
    arrows and a boundary ``left`` stay unbound, so they bubble to whichever
    caller owns the input and mean whatever that caller decides.

    ``WrapMode.CLIP`` holds the input to one row however long the text grows,
    scrolling its view to follow the cursor.
    """
    edit = ReadlineEdit(caption=caption, multiline=False, wrap=wrap)
    edit.keymap["meta left"] = edit.backward_word
    edit.keymap["ctrl left"] = edit.backward_word
    edit.keymap["meta right"] = edit.forward_word
    edit.keymap["ctrl right"] = edit.forward_word
    # ``transpose_chars`` spots a one-character line by comparing the cursor's screen
    # column against 1, which a caption puts out of reach, so it reads past the start
    # of an empty input (raising) and scrambles a short one. Ctrl-T is dropped instead.
    del edit.keymap["ctrl t"]
    return edit


@pure
def _display_width(text: str) -> int:
    """Terminal columns ``text`` occupies, counting a wide character as two."""
    return calc_width(text, 0, len(text))


@pure
def _packed_width(text: str) -> int:
    """Terminal columns ``text`` takes once packed into a slot of its own: its widest line.

    A transient message carries a failed command's raw stderr, which is often
    several lines; measuring the whole run of characters would count them end to
    end and claim a width no widget ever occupies.
    """
    return max(_display_width(line) for line in text.split("\n"))


@pure
def _legend_description_text(description: str) -> str:
    """The part of a legend binding that follows its key: `: description`."""
    return f":{_LEGEND_NBSP}{description.replace(' ', _LEGEND_NBSP)}"


def _legend_markup(
    bindings: Sequence[tuple[str, str]], key_attr: str, text_attr: str, separator: str
) -> list[str | tuple[Hashable, str]]:
    """Text markup for a key legend, one `key: description` unit per binding.

    The key carries ``key_attr`` so it stands out from its description. NBSP inside
    each unit makes it unwrappable, so a narrow footer breaks between bindings
    rather than splitting a key from its description.
    """
    markup: list[str | tuple[Hashable, str]] = []
    for key, description in bindings:
        if markup:
            markup.append((text_attr, separator))
        markup.append((key_attr, key))
        markup.append((text_attr, _legend_description_text(description)))
    return markup


def _panel_hint(bindings: Sequence[tuple[str, str]], hint_attr: str) -> Text:
    """Right-aligned key hint for a panel in the footer slot, inset from its border."""
    return Text(
        [
            *_legend_markup(bindings, "help_key", hint_attr, _PANEL_HINT_SEPARATOR),
            (hint_attr, " "),
        ],
        align="right",
    )


@pure
def _binding_description(cmd: KanpanCommand) -> str:
    """Legend text for a command; a prompted one trails an ellipsis, as a menu item would."""
    if isinstance(cmd, CustomCommand) and cmd.prompt:
        return f"{cmd.name}…"
    return cmd.name


@pure
def _legend_width(bindings: Sequence[tuple[str, str]]) -> int:
    """Rendered width of ``bindings`` laid out the way the belt lays them out, on ``_LEGEND_SEPARATOR``.

    Measured off the markup itself, so the layout is stated once and the width
    cannot drift from what the belt renders.
    """
    markup = _legend_markup(bindings, "", "", _LEGEND_SEPARATOR)
    return _display_width("".join(segment if isinstance(segment, str) else segment[1] for segment in markup))


@pure
def _fit_legend(bindings: Sequence[tuple[str, str]], available_cols: int) -> tuple[tuple[str, str], ...]:
    """Drop whole bindings, leftmost first, until the legend fits ``available_cols``.

    A clipped legend renders fragments -- half of ``r: refresh`` reads as ``resh`` --
    so a binding is either shown whole or dropped. Dropping from the left keeps the
    tail longest, which is why ``?`` is listed last: it is how the keys that were
    dropped can still be found.
    """
    kept = list(bindings)
    while kept and _legend_width(kept) > available_cols:
        kept.pop(0)
    return tuple(kept)


def _build_legend_bindings(
    commands: dict[str, KanpanCommand],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(overlay bindings, footer legend) derived from the live command map.

    The `?` overlay lists every binding -- the fixed interactions, every command
    in the map (builtins and user-configured alike, marks included), and the
    tail. The footer belt advertises only the command keys that are not guessable
    (search/refresh/mute/delete/execute, named from the map so overrides rename
    them); the guessable interactions (space peek, enter attach) stay overlay-only.
    """
    mark_keys = {_BUILTIN_COMMAND_KEY_UNMARK}
    mark_bindings = [
        (key, _binding_description(cmd))
        for key, cmd in commands.items()
        if _mark_color(cmd) is not None or key in mark_keys
    ]
    mark_bindings.append(("U", "unmark all"))
    action_bindings = [
        (key, _binding_description(cmd))
        for key, cmd in commands.items()
        if _mark_color(cmd) is None and key not in mark_keys
    ]
    overlay_bindings = [
        ("space", "peek"),
        ("enter", "attach"),
        *action_bindings,
        *mark_bindings,
        ("q", "quit"),
        ("?", "help"),
    ]
    footer_command_keys = (
        _BUILTIN_COMMAND_KEY_SEARCH,
        _BUILTIN_COMMAND_KEY_REFRESH,
        _BUILTIN_COMMAND_KEY_MUTE,
        _BUILTIN_COMMAND_KEY_DELETE,
        _BUILTIN_COMMAND_KEY_EXECUTE,
    )
    footer_legend = [(key, _binding_description(commands[key])) for key in footer_command_keys if key in commands]
    footer_legend += [("q", "quit"), ("?", "more keys")]
    return overlay_bindings, footer_legend


def _rounded_line_box(body: Any, title: str) -> LineBox:
    """A thin-bordered panel with rounded corners and a left-aligned title."""
    return LineBox(
        body,
        title=title,
        title_align="left",
        tlcorner="╭",
        trcorner="╮",
        blcorner="╰",
        brcorner="╯",
    )


def _install_footer_panel(state: _KanpanState, panel: Any) -> None:
    """Show a panel in the footer slot and give it the keyboard, saving the belt it hides."""
    state.saved_footer = state.frame.footer
    state.frame.footer = panel
    state.frame.focus_position = "footer"


def _restore_footer_belt(state: _KanpanState) -> None:
    """Put the saved keybinding belt back in the footer slot and return focus to the board.

    Restoring focus is not optional: leaving it on the footer makes the board unable
    to receive keys, which reads as a frozen TUI.
    """
    if state.saved_footer is not None:
        state.frame.footer = state.saved_footer
        state.saved_footer = None
    state.frame.focus_position = "body"


def _build_help_overlay(state: _KanpanState) -> Any:
    """A bordered panel over the board listing every key binding, sized to its content.

    Rendered like the peek panel (default background, thin border) with a blank
    row above and below the list and the keys right-aligned into an accented
    column, so it stays readable on light and dark terminals alike.
    """
    key_width = max((len(key) for key, _ in state.legend_bindings), default=1)
    rows: list[Any] = [Divider()]
    rows.extend(
        Text([("help_key", key.rjust(key_width)), "   ", description]) for key, description in state.legend_bindings
    )
    rows.append(Divider())
    listbox: ListBox = ListBox(SimpleFocusListWalker(rows))
    box = _rounded_line_box(Padding(listbox, left=2, right=2), "Keys")
    description_width = max((len(description) for _, description in state.legend_bindings), default=1)
    width = 2 + 2 + key_width + 3 + description_width + 2 + 2
    height = len(rows) + 2
    # Anchored to the lower right, springing from the footer's `?: help`; bottom=2
    # keeps it just above the footer bar and its divider.
    return Overlay(
        box,
        state.frame,
        align="right",
        width=width,
        valign="bottom",
        height=height,
        right=1,
        bottom=2,
    )


def _open_help(state: _KanpanState) -> None:
    """Show the key-binding overlay (`?`); a second `?` or Esc closes it."""
    if state.loop is None or state.help_overlay is not None:
        return
    state.help_overlay = _build_help_overlay(state)
    state.loop.widget = state.help_overlay


def _close_help(state: _KanpanState) -> None:
    if state.loop is None or state.help_overlay is None:
        return
    state.help_overlay = None
    state.loop.widget = state.frame


def _write_terminal_title(screen: BaseScreen, title: str) -> None:
    """Set the terminal window/icon title (OSC 0) through the urwid screen."""
    if isinstance(screen, Screen):
        screen.write(f"\x1b]0;{title}\x07")
        screen.flush()


def _build_peek_panel(state: _KanpanState) -> LineBox:
    """Build the peek panel (a bordered box shown in place of the footer) and stash its parts."""
    state.peek_body_text = Text("", wrap="space")
    state.peek_input = _make_readline_edit(("peek_user", PEEK_REPLY_PROMPT))
    hint = _panel_hint([("enter", "send"), ("esc", "close")], "peek_hint")
    inner = Pile(
        [
            state.peek_body_text,
            Divider(" "),
            state.peek_input,
            hint,
        ]
    )
    # Focus the reply input so typed keys land in it.
    inner.focus_position = 2
    box = _rounded_line_box(inner, "Peek")
    state.peek_box = box
    return box


def _update_peek_header(state: _KanpanState) -> None:
    """Refresh the peek box's border title from the currently peeked agent."""
    if state.peek_box is None:
        return
    entry = _find_entry_by_instance_key(state, state.peek_instance_key)
    if entry is None:
        state.peek_box.set_title("Peek")
        return
    state.peek_box.set_title(f" {entry.name} · {entry.state} · {entry.provider_name} ")


def _set_peek_body(state: _KanpanState) -> None:
    """Render the peek body from the cached transcript, pending replies, and any reply failure."""
    if state.peek_body_text is None:
        return
    markup = _peek_body_markup(state.peek_transcript, state.peek_pending_replies)
    if state.peek_reply_error:
        markup = [*markup, "\n", ("peek_hint", f"(reply failed: {state.peek_reply_error})")]
    state.peek_body_text.set_text(markup)


def _cancel_peek_alarm(state: _KanpanState) -> None:
    """Cancel any pending live-capture alarm for the peek panel."""
    if state.peek_alarm is not None and state.loop is not None:
        state.loop.remove_alarm(state.peek_alarm)
    state.peek_alarm = None


def _start_peek_capture(state: _KanpanState) -> None:
    """Kick off a background transcript read for the peeked agent and poll for it."""
    if state.peek_instance_key is None or state.loop is None:
        return
    executor = _ensure_peek_executor(state)
    state.peek_capture_future = executor.submit(_run_transcript, str(state.peek_instance_key))
    state.peek_alarm = state.loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_peek_capture_poll, state)


def _on_peek_capture_poll(loop: MainLoop, state: _KanpanState) -> None:
    """Poll the in-flight transcript read; render it and schedule the next while open."""
    state.peek_alarm = None
    future = state.peek_capture_future
    if future is None or state.peek_agent_name is None:
        return
    if not future.done():
        state.peek_alarm = loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_peek_capture_poll, state)
        return

    state.peek_capture_future = None
    try:
        result = future.result()
    except (subprocess.SubprocessError, OSError) as e:
        if state.peek_body_text is not None:
            state.peek_body_text.set_text(("peek_hint", f"(transcript failed: {e})"))
    else:
        if result.returncode != 0:
            detail = _last_nonempty_line(result.stderr) or f"exited with code {result.returncode}"
            if state.peek_body_text is not None:
                state.peek_body_text.set_text(("peek_hint", f"(no transcript: {detail})"))
        else:
            state.peek_transcript = result.stdout
            # A reply the agent has since accepted now appears in the transcript, so drop
            # its optimistic echo to avoid showing it twice.
            delivered = set(result.stdout.splitlines())
            state.peek_pending_replies = [r for r in state.peek_pending_replies if r not in delivered]
            _set_peek_body(state)
    if state.peek_agent_name is not None:
        state.peek_alarm = loop.set_alarm_in(PEEK_REFRESH_SECONDS, _on_peek_capture_tick, state)


def _on_peek_capture_tick(loop: MainLoop, state: _KanpanState) -> None:
    """Alarm callback that starts the next live capture."""
    state.peek_alarm = None
    if state.peek_agent_name is not None:
        _start_peek_capture(state)


def _open_peek(state: _KanpanState) -> None:
    """Open the peek panel for the focused agent."""
    entry = _get_focused_entry(state)
    if entry is None:
        return
    state.peek_agent_name = entry.name
    state.peek_instance_key = entry.instance_key
    state.focused_instance_key = entry.instance_key
    state.peek_transcript = ""
    state.peek_pending_replies = []
    state.peek_reply_error = ""
    _install_footer_panel(state, _build_peek_panel(state))
    _update_peek_header(state)
    if state.peek_body_text is not None:
        state.peek_body_text.set_text(("peek_hint", "(loading...)"))
    _start_peek_capture(state)


def _close_peek(state: _KanpanState) -> None:
    """Close the peek panel and restore the footer and board focus."""
    if state.peek_agent_name is None:
        return
    closed_instance_key = state.peek_instance_key
    _cancel_peek_alarm(state)
    state.peek_capture_future = None
    state.peek_agent_name = None
    state.peek_instance_key = None
    _restore_footer_belt(state)
    state.focused_instance_key = closed_instance_key
    if closed_instance_key is not None:
        _focus_row_by_instance_key(state, closed_instance_key)
    state.peek_box = None
    state.peek_body_text = None
    state.peek_input = None
    state.peek_transcript = ""
    state.peek_pending_replies = []
    state.peek_reply_error = ""


def _toggle_peek(state: _KanpanState) -> None:
    """Toggle the peek panel for the focused agent."""
    if state.peek_agent_name is not None:
        _close_peek(state)
    else:
        _open_peek(state)


def _submit_peek_reply(state: _KanpanState) -> None:
    """Send the reply-input text to the peeked agent and echo it immediately; no-op when empty.

    ``mngr message`` blocks until durable evidence shows the agent accepted the reply,
    up to ~90s, and longer still when the agent has to be started first -- so the send
    runs on the reply executor and is not awaited. The typed text is echoed into the body
    right away (as a ``›`` line) and, once the agent accepts it and it shows up in the
    transcript, the echo is dropped in favour of the real message.
    """
    if state.peek_agent_name is None or state.peek_instance_key is None or state.peek_input is None:
        return
    text = state.peek_input.get_edit_text().strip()
    if not text:
        return
    state.peek_pending_replies = [*state.peek_pending_replies, text]
    state.peek_reply_error = ""
    executor = _ensure_peek_reply_executor(state)
    state.peek_reply_future = executor.submit(_run_message, str(state.peek_instance_key), text)
    state.peek_input.set_edit_text("")
    _set_peek_body(state)
    if state.loop is not None:
        state.loop.set_alarm_in(
            SPINNER_INTERVAL_SECONDS,
            _on_peek_reply_poll,
            (state, state.peek_reply_future, state.peek_instance_key, state.peek_agent_name, text),
        )


def _on_peek_reply_poll(
    loop: MainLoop,
    data: tuple[_KanpanState, Future[subprocess.CompletedProcess[str]], AgentInstanceKey, AgentName, str],
) -> None:
    """Poll a sent reply; refresh the board either way, drop its optimistic echo on failure.

    A delivered reply keeps its echo until the transcript refresh prunes it, and re-probes because
    accepting the message puts a WAITING agent back to work. A failed one re-probes because it may
    have moved the row too: a STOPPED or DONE agent is (re)launched before delivery is even
    attempted, and the exit code does not say whether it got that far. A failed send would
    otherwise leave the echo up forever, showing the message as delivered when it was not.
    """
    state, future, instance_key, agent_name, reply_text = data
    if not future.done():
        loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_peek_reply_poll, data)
        return
    try:
        result = future.result()
    except (subprocess.SubprocessError, OSError) as e:
        detail = str(e)
    else:
        if result.returncode == 0:
            _request_local_refresh(loop, state)
            return
        detail = _last_nonempty_line(result.stderr) or f"exited with code {result.returncode}"
    if state.peek_instance_key == instance_key:
        pending = list(state.peek_pending_replies)
        if reply_text in pending:
            pending.remove(reply_text)
            state.peek_pending_replies = pending
        state.peek_reply_error = detail
        _set_peek_body(state)
    elif state.peek_agent_name is not None:
        # Another agent's panel now occupies the footer slot (hiding the footer), so its
        # error line is the only visible surface; name the failed agent to keep the two apart.
        state.peek_reply_error = f"{agent_name}: {detail}"
        _set_peek_body(state)
    else:
        # The panel has closed, so the restored footer is visible again and is the
        # right place for the failure notice.
        _show_transient_message(state, f"  Reply to {agent_name} failed: {detail}")
    _request_local_refresh(loop, state)


def _handle_peek_key(state: _KanpanState, key: str) -> bool | None:
    """Route keys while the peek panel is open. Printable keys reach the reply Edit."""
    if key in ("esc", "ctrl c"):
        _close_peek(state)
        return True
    if key == "enter":
        _submit_peek_reply(state)
        return True
    return None


@pure
def _rank_matches(rows: Sequence[tuple[AgentName, str]], query: str) -> tuple[AgentName, ...]:
    """Rank ``(name, row_text)`` pairs against ``query``, best match first.

    ``row_text`` is every rendered cell of the row joined together, so a query
    matches an agent by name, PR number, CI status, or any other column. Rank
    tiers: name prefix, then name substring, then anywhere else in the row.
    Ties keep board order.
    """
    needle = query.strip().lower()
    if not needle:
        return ()
    ranked: list[tuple[int, int, AgentName]] = []
    for position, (name, row_text) in enumerate(rows):
        lowered_name = str(name).lower()
        if lowered_name.startswith(needle):
            tier = 0
        elif needle in lowered_name:
            tier = 1
        elif needle in row_text.lower():
            tier = 2
        else:
            continue
        ranked.append((tier, position, name))
    return tuple(name for _, _, name in sorted(ranked))


def _search_rows(state: _KanpanState) -> list[tuple[AgentName, str]]:
    """``(name, all rendered cell text)`` for every displayed row, in board order."""
    return [
        (entry.name, " ".join(defn.text_fn(entry) for defn in state.column_defs))
        for _, entry in sorted(state.index_to_entry.items())
    ]


def _search_counter_text(state: _KanpanState, query: str) -> str:
    """How the prompt reports its matches: ``2/6``, ``no match``, or nothing yet."""
    if not query.strip():
        return ""
    if not state.search_matches:
        return "no match"
    return f"{state.search_index + 1}/{len(state.search_matches)}"


def _build_footer(footer_left_attr: AttrMap) -> tuple[Pile, Columns, Text]:
    """The footer -- a blank row above the belt -- the belt itself, and its legend widget.

    All three are addressed positionally, by ``_FOOTER_BELT_ROW`` and by the belt's
    ``_FOOTER_STATUS_SLOT`` and ``_FOOTER_LEGEND_SLOT``, so this shape is stated
    here alone. One AttrMap over the whole row keeps the belt continuous (no
    unpainted gap between the left status and the right legend). The legend clips
    rather than wraps, holding the footer to its two rows however wide the legend
    is written; ``_fit_legend`` is what keeps it from reaching that clip.
    """
    footer_right = Text("", align="right", wrap=WrapMode.CLIP)
    footer_items: list[Any] = [("pack", footer_left_attr), footer_right]
    belt = Columns(footer_items, dividechars=_FOOTER_DIVIDECHARS)
    return Pile([Divider(), AttrMap(belt, "footer")]), belt, footer_right


def _set_belt_status_slot(state: _KanpanState, status_widget: Any, *, is_status_flexible: bool) -> None:
    """Put ``status_widget`` in the belt's status slot, and hand the spare width to one slot or the other.

    Exactly one slot is flexible: the open search prompt grows into the spare width
    while its legend packs, and the board's status text packs while its legend takes
    the rest, which is the width ``_legend_available_cols`` fits the legend to.
    """
    columns = state.footer_columns
    if columns is None:
        return
    packed = columns.options("pack")
    flexible = columns.options("weight", 1)
    columns.contents[_FOOTER_STATUS_SLOT] = (status_widget, flexible if is_status_flexible else packed)
    columns.contents[_FOOTER_LEGEND_SLOT] = (state.footer_right, packed if is_status_flexible else flexible)
    _resync_footer_selectability(state)


def _legend_available_cols(state: _KanpanState) -> int:
    """Columns the belt's legend may use, after whatever sits to its left.

    While the prompt is open the query needs room of its own; otherwise the legend
    yields to however wide the status text currently reads.
    """
    if state.loop is None:
        return _LEGEND_UNMEASURED_COLS
    cols, _rows = state.loop.screen.get_cols_rows()
    if state.search_input is not None:
        left_cols = _SEARCH_QUERY_MIN_COLS
    else:
        left_cols = _packed_width(state.footer_left_text.get_text()[0])
    return cols - left_cols - _FOOTER_DIVIDECHARS - len(_LEGEND_TRAILING_PAD)


def _set_footer_legend(state: _KanpanState, bindings: Sequence[tuple[str, str]], prefix: str = "") -> None:
    """Remember the belt's legend and render as much of it as fits."""
    state.active_legend = list(bindings)
    state.active_legend_prefix = prefix
    _render_footer_legend(state)


def _render_footer_legend(state: _KanpanState) -> None:
    """Write the belt's legend, dropping whole bindings that the width cannot hold."""
    prefix = state.active_legend_prefix
    lead: list[str | tuple[Hashable, str]] = [("footer", f"{prefix}{_LEGEND_SEPARATOR}")] if prefix else []
    lead_cols = _display_width(prefix) + len(_LEGEND_SEPARATOR) if prefix else 0
    bindings = _fit_legend(state.active_legend, _legend_available_cols(state) - lead_cols)
    state.footer_right.set_text(
        [
            *lead,
            *_legend_markup(bindings, "footer_key", "footer", _LEGEND_SEPARATOR),
            ("footer", _LEGEND_TRAILING_PAD),
        ]
    )


def _set_search_belt(state: _KanpanState, query: str) -> None:
    """Belt content while the prompt is open: the match counter, then the prompt's keys."""
    _set_footer_legend(state, _SEARCH_LEGEND, _search_counter_text(state, query))


def _resync_footer_selectability(state: _KanpanState) -> None:
    """Tell the footer Pile that the belt it holds swapped a widget.

    ``Pile`` caches whether it is selectable and recomputes only when its own
    contents list is assigned to, so a swap inside the belt leaves that cache
    stale. ``Frame.keypress`` skips a footer reporting itself unselectable, which
    would leave the prompt visible but unable to receive a single keystroke.
    """
    pile = state.footer_pile
    if pile is None:
        return
    pile.contents[_FOOTER_BELT_ROW] = pile.contents[_FOOTER_BELT_ROW]


def _highlight_search_match(state: _KanpanState, name: AgentName | None) -> None:
    """Paint the row for ``name`` as if it were focused, clearing any previous highlight.

    The prompt holds the keyboard while a search is open, so the board's ListBox
    renders unfocused and its own focus attributes never appear. Mapping the row's
    attributes to their focus variants puts the highlight back under the match.
    """
    if state.list_walker is None:
        return
    focus_map = _build_focus_map(state.mark_attr_names, state.col_attr_names)
    for idx, entry in state.index_to_entry.items():
        state.list_walker[idx].set_attr_map(focus_map if entry.name == name else {None: None})


def _highlight_search_selection(state: _KanpanState) -> None:
    """Paint whichever row the board has selected, so the prompt never hides where the user is."""
    entry = _get_focused_entry(state)
    _highlight_search_match(state, entry.name if entry is not None else None)


def _restore_pre_search_focus(state: _KanpanState) -> None:
    """Put the board back where the search found it: on a row, or on no row at all.

    ``up`` at the board's top row clears the selection, so a search can open with no
    row to come back to, and a refresh landing mid-search can take that row off the
    board. Either way there is nothing to come back to, and coming back means
    clearing -- never leaving the match selected, which is what committing looks like.
    """
    if state.pre_search_focus is None or _find_entry_by_name(state, state.pre_search_focus) is None:
        _clear_focus(state)
        return
    _focus_row_by_name(state, state.pre_search_focus)


def _apply_search_query(state: _KanpanState, query: str, *, keep_match: AgentName | None = None) -> None:
    """Re-rank the board against ``query`` and jump to the best match.

    An erased query puts the board back exactly as the prompt found it, so a
    query typed and then deleted is indistinguishable from one never typed. A
    query that simply matches nothing leaves the selection alone -- unless a
    rebuilt board has taken it away, which has to be re-asserted rather than
    left to urwid: a fresh ``ListBox`` claims the first selectable row on its
    next render, silently selecting a row the belt never called a match.

    ``keep_match`` stays selected if it survives the re-ranking, so re-running
    the query over a rebuilt board leaves the user on the match they stepped to.
    """
    state.search_matches = _rank_matches(_search_rows(state), query)
    state.search_index = state.search_matches.index(keep_match) if keep_match in state.search_matches else 0
    match = state.search_matches[state.search_index] if state.search_matches else None
    if match is not None:
        _focus_row_by_name(state, match)
    elif not query.strip() or _get_focused_entry(state) is None:
        _restore_pre_search_focus(state)
    else:
        # The board still holds the row it held before this keystroke, and a
        # fruitless query is meant to leave it there.
        pass
    _highlight_search_selection(state)
    _set_search_belt(state, query)


def _on_search_text_change(state: _KanpanState, widget: Edit, new_text: str) -> None:
    """Signal callback fired on every keystroke in the search input."""
    _apply_search_query(state, new_text)


class _SearchBackspace(MutableModel):
    """Backspace for the search prompt, which erases the ``/`` once the query is empty.

    ``ReadlineEdit`` consumes backspace whether or not it had anything to delete,
    so backing out of the prompt has to be handled here rather than downstream.
    Deleting the last character leaves an open, empty prompt; the next backspace
    takes the ``/`` with it and cancels, so the key retraces exactly what it typed.
    """

    state: _KanpanState
    backward_delete_char: Callable[[], None]

    def __call__(self) -> None:
        search_input = self.state.search_input
        if search_input is not None and search_input.get_edit_text():
            self.backward_delete_char()
            return
        _close_search(self.state, is_cancelled=True)


def _cycle_search(state: _KanpanState, delta: int) -> None:
    """Focus the next (or previous) match, wrapping at the ends."""
    if state.search_input is None or not state.search_matches:
        return
    state.search_index = (state.search_index + delta) % len(state.search_matches)
    _focus_row_by_name(state, state.search_matches[state.search_index])
    _highlight_search_selection(state)
    _set_search_belt(state, state.search_input.get_edit_text())


def _open_search(state: _KanpanState) -> None:
    """Open the incremental search prompt in the footer's status slot.

    The prompt takes over the slot that carries the refresh stamp rather than
    adding a row, so a search never moves the board under the cursor -- which is
    also why the query is clipped to its one row rather than wrapping onto a
    second. The query input gets the same readline editing as the peek reply
    input; ``up``/``down``/``enter``/``esc`` stay unbound so they reach the board
    as match cycling, commit, and cancel.
    """
    if state.search_input is not None or state.footer_columns is None:
        return
    entry = _get_focused_entry(state)
    state.pre_search_focus = entry.name if entry is not None else None
    state.search_matches = ()
    state.search_index = 0
    edit = _make_readline_edit(("footer_key", "  /"), wrap=WrapMode.CLIP)
    connect_signal(edit, "change", _on_search_text_change, user_args=[state])
    for backspace_key in ("backspace", "ctrl h"):
        # urwid_readline types its keymap from the bound methods it ships with, so it
        # does not admit an external callable even though it is meant to be rebound.
        edit.keymap[backspace_key] = _SearchBackspace(  # ty: ignore[invalid-assignment]
            state=state, backward_delete_char=edit.keymap[backspace_key]
        )
    state.search_input = edit
    # The query grows into the belt's free space while the legend shrinks to fit.
    _set_belt_status_slot(state, AttrMap(edit, "footer"), is_status_flexible=True)
    state.footer_columns.focus_position = _FOOTER_STATUS_SLOT
    if state.footer_pile is not None:
        state.footer_pile.focus_position = _FOOTER_BELT_ROW
    state.frame.focus_position = "footer"
    # The ListBox renders unfocused from here on, so the row the user was on has to
    # be painted explicitly or pressing `/` would wipe their place off the board.
    _highlight_search_selection(state)
    # The board's keys do nothing while the prompt has the keyboard, so the belt
    # advertises the prompt's keys instead of a legend that would not fire.
    _set_search_belt(state, "")


def _close_search(state: _KanpanState, *, is_cancelled: bool) -> None:
    """Close the search prompt, keeping the matched row focused unless cancelled."""
    if state.search_input is None:
        return
    # Cleared before the belt is repainted: the legend is fitted around whatever
    # sits to its left, which is the status text again as soon as this is None.
    state.search_input = None
    state.search_matches = ()
    state.search_index = 0
    _set_belt_status_slot(state, state.footer_left_attr, is_status_flexible=False)
    # The board takes the keyboard back, so its own focus attributes render again.
    _highlight_search_match(state, None)
    _set_footer_legend(state, state.footer_legend)
    state.frame.focus_position = "body"
    if is_cancelled:
        _restore_pre_search_focus(state)
    state.pre_search_focus = None
    # The board's own focus memory must follow the search, or the next refresh
    # snaps back to whichever row was focused before -- including when the search
    # ends on no row at all, which is a selection the memory has to record too.
    focused = _get_focused_entry(state)
    state.focused_instance_key = focused.instance_key if focused is not None else None


def _handle_search_key(state: _KanpanState, key: str) -> bool | None:
    """Route the keys that bubbled past the open search prompt; its Edit already took the printable ones."""
    if key in ("esc", "ctrl c"):
        _close_search(state, is_cancelled=True)
        return True
    if key == "enter":
        _close_search(state, is_cancelled=False)
        return True
    if key == "down":
        _cycle_search(state, 1)
        return True
    if key == "up":
        _cycle_search(state, -1)
        return True
    return None


def _dispatch_command(state: _KanpanState, key: str, cmd: KanpanCommand) -> None:
    """Dispatch a command by key."""
    if isinstance(cmd, MarkableBuiltinCommand):
        _toggle_mark(state, key)
        return
    if isinstance(cmd, CustomCommand):
        if _mark_color(cmd) is not None:
            _toggle_mark(state, key)
            return
        if cmd.command:
            if cmd.prompt:
                _open_prompt_for_command(state, cmd)
            else:
                _run_shell_command(state, cmd)
        return
    # cmd is ActionBuiltinCommand; match on role for exhaustive dispatch.
    match cmd.role:
        case ActionBuiltinRole.REFRESH:
            if state.loop is not None and state.refresh_future is None:
                _start_refresh(state.loop, state)
        case ActionBuiltinRole.MUTE:
            _mute_focused_agent(state)
        case ActionBuiltinRole.UNMARK:
            _unmark_focused(state)
        case ActionBuiltinRole.EXECUTE:
            _execute_marks(state)
        case ActionBuiltinRole.SEARCH:
            _open_search(state)
        case _:
            assert_never(cmd.role)


def _launch_custom_command(
    state: _KanpanState,
    executor: ThreadPoolExecutor,
    cmd: CustomCommand,
    agent_name: AgentName,
    instance_key: AgentInstanceKey,
    input_text: str,
) -> None:
    """Start a custom command in the background, showing the spinner and polling for it."""
    state.action_label = f"  Running {cmd.name} on {agent_name}"
    _render_footer(state)
    _ensure_animation_running(state)
    future = executor.submit(_run_shell_command_sync, cmd.command, str(agent_name), input_text, instance_key)
    if state.loop is not None:
        state.loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_custom_command_poll, (state, future, cmd, agent_name))


def _run_shell_command(state: _KanpanState, cmd: CustomCommand) -> None:
    """Run a non-prompted custom command on the focused agent."""
    entry = _get_focused_entry(state)
    if entry is None:
        return
    if state.executor is None:
        state.executor = ThreadPoolExecutor(max_workers=1)
    _launch_custom_command(state, state.executor, cmd, entry.name, entry.instance_key, "")


def _ensure_batch_executor(state: _KanpanState) -> ThreadPoolExecutor:
    """Return the batch executor, creating it on first use."""
    if state.batch_executor is None:
        state.batch_executor = ThreadPoolExecutor(max_workers=state.batch_concurrency)
    return state.batch_executor


def _ensure_prompt_executor(state: _KanpanState) -> ThreadPoolExecutor:
    """Return the single-worker prompted-command executor, creating it on first use."""
    if state.prompt_executor is None:
        state.prompt_executor = ThreadPoolExecutor(max_workers=1)
    return state.prompt_executor


class _RunPromptedCommand(FrozenModel):
    """Prompt handler that runs a custom command with the submitted text as MNGR_INPUT."""

    state: _KanpanState
    cmd: CustomCommand
    agent_name: AgentName
    instance_key: AgentInstanceKey

    def __call__(self, input_text: str) -> None:
        _launch_custom_command(
            self.state, _ensure_prompt_executor(self.state), self.cmd, self.agent_name, self.instance_key, input_text
        )


def _open_prompt_for_command(state: _KanpanState, cmd: CustomCommand) -> None:
    """Ask for a prompted command's input, bound to the agent focused right now."""
    entry = _get_focused_entry(state)
    if entry is None:
        return
    state.focused_instance_key = entry.instance_key
    # Title names only the agent: the caption is the command's own `prompt` text, which
    # already says what is being asked, so repeating the command name reads as a stutter.
    _open_prompt(
        state,
        title=f" {entry.name} ",
        caption=cmd.prompt,
        on_submit=_RunPromptedCommand(state=state, cmd=cmd, agent_name=entry.name, instance_key=entry.instance_key),
    )


def _build_prompt_overlay(state: _KanpanState, title: str, edit: ReadlineEdit) -> Overlay:
    """A compact bordered input floating in the middle of the board.

    Centred rather than parked in the footer slot because the prompt is modal -- board
    keys and clicks are both withheld while it is open -- and sized to the input rather
    than to the terminal, so a wide screen does not stretch one short line across it.
    """
    hint = _panel_hint([("enter", "apply"), ("esc", "cancel")], "prompt_hint")
    inner = Pile([edit, hint])
    # Focus the input so typed keys land in it.
    inner.focus_position = 0
    return Overlay(
        _rounded_line_box(Padding(inner, left=1, right=1), title),
        state.frame,
        align="center",
        width=_PROMPT_WIDTH,
        valign="middle",
        height="pack",
    )


def _open_prompt(state: _KanpanState, title: str, caption: str, on_submit: Callable[[str], None]) -> None:
    """Ask for one line of text in a centred box, to be answered by ``on_submit``.

    Nothing here knows what the answer drives -- callers bind their target when they
    build the handler. ``title`` names that target; the board's own selection highlight
    is not drawn while the overlay holds focus, so the title is the only cue for it.
    """
    # A second prompt would replace the first as the loop's widget, stranding it open in
    # state with no way to reach or dismiss it.
    if state.open_prompt is not None:
        return
    edit = _make_readline_edit(("prompt_caption", caption))
    state.open_prompt = _OpenPrompt(edit=edit, on_submit=on_submit)
    if state.loop is not None:
        state.loop.widget = _build_prompt_overlay(state, title, edit)


def _close_prompt(state: _KanpanState) -> None:
    """Close the prompt, handing the screen back to the board."""
    if state.open_prompt is None:
        return
    state.open_prompt = None
    if state.loop is not None:
        state.loop.widget = state.frame


def _submit_prompt(state: _KanpanState) -> None:
    """Hand the typed text to the open prompt's handler, closing the prompt first.

    An empty line is a valid submission -- ``mngr label X -l "tag="`` is the only way to
    clear a label -- so Esc rather than an empty Enter carries the cancel meaning.
    Closing first means the handler's own footer messages are visible as it runs.
    """
    open_prompt = state.open_prompt
    if open_prompt is None:
        return
    input_text = open_prompt.edit.get_edit_text()
    _close_prompt(state)
    open_prompt.on_submit(input_text)


def _handle_prompt_key(state: _KanpanState, key: str) -> bool | None:
    """Route keys while a prompt is open. Printable keys reach the input Edit."""
    if key in ("esc", "ctrl c"):
        _close_prompt(state)
        return True
    if key == "enter":
        _submit_prompt(state)
        return True
    return None


def _on_custom_command_poll(
    loop: MainLoop, data: tuple[_KanpanState, Future[subprocess.CompletedProcess[str]], CustomCommand, AgentName]
) -> None:
    """Poll for custom command completion."""
    state, future, cmd, agent_name = data
    if not future.done():
        loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_custom_command_poll, data)
        return
    try:
        result = future.result()
        if result.returncode == 0:
            message = f"  {cmd.name} completed for {agent_name}"
        else:
            message = f"  {cmd.name} failed for {agent_name}: {result.stderr.strip()}"
    except Exception as e:
        message = f"  {cmd.name} failed for {agent_name}: {e}"
    if cmd.refresh_afterwards:
        _report_after_refresh(state, message, loop)
    else:
        state.action_label = None
        _show_transient_message(state, message)


def _refresh_stamp(seconds_ago: float, fetch_seconds: float | None) -> str:
    """Relative footer stamp for the last full refresh, e.g. `  Refreshed 5m ago`.

    The fetch duration only shows in the just-now window, so it fades once stale.
    """
    if seconds_ago < _STAMP_JUST_NOW_SECONDS:
        took = f" \u00b7 {fetch_seconds:.1f}s" if fetch_seconds is not None else ""
        return f"  Refreshed just now{took}"
    if seconds_ago < 60:
        return f"  Refreshed {int(seconds_ago)}s ago"
    if seconds_ago < 3600:
        return f"  Refreshed {int(seconds_ago // 60)}m ago"
    return f"  Refreshed {int(seconds_ago // 3600)}h ago"


def _update_refresh_stamp(state: _KanpanState) -> None:
    """Recompute the steady footer text from the time of the last full refresh that landed.

    Before any has, there is no age to report and the footer keeps whatever it is showing.
    """
    if not state.last_successful_refresh_time:
        return
    seconds_ago = time.monotonic() - state.last_successful_refresh_time
    state.steady_footer_text = _refresh_stamp(seconds_ago, state.last_fetch_seconds)


def _on_stamp_tick(loop: MainLoop, state: _KanpanState) -> None:
    """Alarm callback: age the relative refresh stamp and re-render the footer."""
    _update_refresh_stamp(state)
    _render_footer(state)
    loop.set_alarm_in(_STAMP_TICK_SECONDS, _on_stamp_tick, state)


def _marks_footer_text(state: _KanpanState) -> str:
    """Build the footer text summarizing the currently marked agents."""
    counts: dict[str, int] = {}
    for mark_key in state.marks.values():
        counts[mark_key] = counts.get(mark_key, 0) + 1
    parts = []
    for mark_key, count in sorted(counts.items()):
        cmd = state.commands.get(mark_key)
        label = cmd.name if cmd else mark_key
        parts.append(f"{count} {label}")
    return f"  Marked: {', '.join(parts)}  (x to execute, U to unmark all)"


def _compute_footer_display(state: _KanpanState) -> tuple[str, str]:
    """Return the (text, palette attr) the footer-left should show, by priority.

    Priority, highest first: a transient notification, an in-progress user action
    (batch/custom command), a background refresh, the marked-agents summary, then
    the steady-state text. Only one of these owns the widget at a time, so the
    several alarm loops that drive them can no longer overwrite each other.
    """
    if state.transient_message is not None:
        return state.transient_message, "notification"
    frame_char = SPINNER_FRAMES[state.spinner_index % len(SPINNER_FRAMES)]
    if state.action_label is not None:
        return f"{state.action_label} {frame_char}", "footer"
    if state.refresh_future is not None:
        return f"  Refreshing {frame_char}", "footer"
    if state.marks:
        return _marks_footer_text(state), "footer"
    return state.steady_footer_text, "footer"


def _render_footer(state: _KanpanState) -> None:
    """Write the footer-left widget from current state, and re-fit the belt legend beside it.

    The sole writer of the footer-left widget. The legend is fitted to whatever room
    that text leaves, so the two are written together -- which is also what a window
    resize goes through to re-fit the belt.
    """
    text, attr = _compute_footer_display(state)
    state.footer_left_text.set_text(text)
    state.footer_left_attr.set_attr_map({None: attr})
    _render_footer_legend(state)


def _build_header(title: str) -> tuple[Pile, _FitOrHideText]:
    """Build the header -- the title centred on the screen -- and its status widget.

    The two equal-weight cells split whatever the packed title leaves, so the
    title stays centred however wide the status text grows. `min_width=0` leaves
    them no width of their own, so the title keeps every column it needs and
    wraps rather than vanishing on a narrow terminal.
    """
    status_text = _FitOrHideText("", align="right", wrap="clip")
    header_items: list[Any] = [Text(""), ("pack", Text(title)), status_text]
    header = Pile(
        [
            AttrMap(Columns(header_items, min_width=0), "header"),
            Divider(),
        ]
    )
    return header, status_text


def _render_header_status(state: _KanpanState) -> None:
    """Write the header's status widget from the current snapshot. The sole writer of that widget."""
    text = render_header_status(state.header_status, state.snapshot, state.section_order)
    state.header_status_text.set_text(f"{_HEADER_STATUS_PAD}{text}{_HEADER_STATUS_PAD}" if text else "")


def _ensure_animation_running(state: _KanpanState) -> None:
    """Start the single spinner-animation tick if it is not already running."""
    if state.animation_alarm is None and state.loop is not None:
        state.animation_alarm = state.loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_animation_tick, state)


def _on_animation_tick(loop: MainLoop, state: _KanpanState) -> None:
    """Advance the spinner and re-render; reschedule while any animated work is active."""
    state.animation_alarm = None
    state.spinner_index += 1
    _render_footer(state)
    if state.action_label is not None or state.refresh_future is not None:
        state.animation_alarm = loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _on_animation_tick, state)


def _show_transient_message(state: _KanpanState, message: str) -> None:
    """Show a transient notification in the footer that auto-reverts after a few seconds."""
    state.transient_message = message
    if state.loop is not None:
        if state.transient_alarm is not None:
            state.loop.remove_alarm(state.transient_alarm)
        state.transient_alarm = state.loop.set_alarm_in(TRANSIENT_MESSAGE_SECONDS, _on_transient_expire, state)
    _render_footer(state)


def _report_after_refresh(state: _KanpanState, message: str, loop: MainLoop) -> None:
    """Hold `message` until a refresh has repainted the board, then show it.

    A command that repaints produced two beats otherwise: the outcome, then a second
    later the rows it was describing. Holding it keeps the in-progress label up across
    both, so the board changes and says why in one step.
    """
    state.pending_completion_message = message
    _request_local_refresh(loop, state)


def _is_repaint_outstanding(state: _KanpanState) -> bool:
    """Whether a refresh that can show the board's latest change has yet to land.

    A refresh in flight alongside a held request was started before that request, and so
    before the change it was made for.
    """
    return state.refresh_future is not None or state.is_local_refresh_pending


def _flush_pending_completion(state: _KanpanState) -> None:
    """Show the outcome a command left waiting for its repaint, if there is one.

    A notification already on the footer is left to finish: that slot answers whatever the user
    did most recently, so an outcome from before it waits for the slot rather than taking it.
    The command is over either way, so its in-progress label goes now.
    """
    message = state.pending_completion_message
    if message is None:
        return
    state.action_label = None
    if state.transient_message is not None:
        _render_footer(state)
        return
    state.pending_completion_message = None
    _show_transient_message(state, message)


def _on_transient_expire(loop: MainLoop, state: _KanpanState) -> None:
    """Alarm callback: clear the transient notification and re-render.

    Freeing the slot releases an outcome whose repaint has landed. One still waiting for its
    repaint keeps waiting, and `_finish_refresh` releases it into the slot this frees.
    """
    state.transient_alarm = None
    state.transient_message = None
    if not _is_repaint_outstanding(state):
        _flush_pending_completion(state)
    _render_footer(state)


def _request_refresh(loop: MainLoop, state: _KanpanState, cooldown_seconds: float) -> None:
    """Request a refresh, subject to a cooldown period."""
    if state.refresh_future is not None:
        return
    elapsed = time.monotonic() - state.last_refresh_attempt_time
    remaining = cooldown_seconds - elapsed
    if remaining <= 0:
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


def _request_local_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Run a local-only refresh, deferring it until any in-flight refresh finishes.

    The single entry point for every action that changes what the board should show. An
    in-flight refresh was started before the action happened, so its result cannot contain
    the change; holding the request and re-running it afterwards is what makes the caller's
    repaint certain rather than dependent on refresh timing.
    """
    if state.refresh_future is not None:
        state.is_local_refresh_pending = True
        return
    _start_local_refresh(loop, state)


def _on_local_refresh_alarm(loop: MainLoop, state: _KanpanState) -> None:
    """Alarm callback for the periodic local refresh.

    A tick that lands while the previous one is still running is skipped rather than
    queued, so the interval is a floor on how often the board re-reads rather than a
    promise, and an interval shorter than a refresh takes degrades to back-to-back
    refreshes instead of a growing backlog. The alarm re-arms either way.

    A full refresh in flight counts as this tick: it fetches everything this one would and
    more, so running alongside it would only sweep the agent list twice for one answer. The
    deference is one-way -- the full refresh reads `refresh_future` alone, so however often
    this one runs it can neither take that field nor keep a full refresh from starting.
    """
    if state.local_refresh_future is None and state.refresh_future is None:
        _start_periodic_local_refresh(loop, state)
    _schedule_next_local_refresh(loop, state)


def _schedule_next_local_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Arm the next periodic local refresh, unless the interval asks for none."""
    if state.local_refresh_interval_seconds <= 0:
        return
    loop.set_alarm_in(state.local_refresh_interval_seconds, _on_local_refresh_alarm, state)


def _start_periodic_local_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Start the periodic local refresh in the background.

    Runs on its own worker and tracks its own future, so at an interval short enough to keep
    it running most of the time it neither waits behind the full refresh nor keeps that
    refresh out of `refresh_future`.
    """
    if state.local_refresh_executor is None:
        state.local_refresh_executor = ThreadPoolExecutor(max_workers=1)
    state.local_refresh_future = state.local_refresh_executor.submit(
        fetch_local_snapshot,
        state.mngr_ctx,
        state.data_sources,
        state.cached_fields,
        state.include_filters,
        state.exclude_filters,
    )
    loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _poll_periodic_local_refresh, state)


def _poll_periodic_local_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Alarm callback: watch the periodic local refresh and apply it when it lands."""
    future = state.local_refresh_future
    if future is None:
        return
    if not future.done():
        loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _poll_periodic_local_refresh, state)
        return
    state.local_refresh_future = None
    try:
        fresh = future.result().snapshot
    except MngrError as e:
        # The board is left exactly as it was: this runs unprompted and often, so an error row
        # per tick would be noise, and the full refresh surfaces trouble that persists within
        # `refresh_interval_seconds`. Only a fetch failure is swallowed; anything else reaches
        # the loop as the bug it is. `run_kanpan` disables logging for as long as it holds the
        # terminal, so this line reaches a sink only for a caller that is not the board.
        logger.debug("Periodic local refresh failed: {}", e)
        return
    if fresh.errors:
        # Shown nowhere, for the same reason and with the same reach as the failure above.
        logger.debug("Periodic local refresh reported errors: {}", fresh.errors)
    previous = state.snapshot
    if previous is None:
        state.snapshot = fresh
    else:
        # Only remote sources sat this one out, so their columns come from the last full
        # refresh and keep its cadence while everything local tracks the interval. The error
        # list stays the full refresh's too: this read did run sources that fill one, but
        # taking its list would drop what a remote source reported, and only that refresh
        # runs those sources and can put it back.
        merged = _prefer_later_read(previous, _carry_forward_fields(previous, fresh), _LOCALLY_WRITTEN_FIELDS)
        state.snapshot = merged.model_copy_update(to_update(merged.field_ref().errors, previous.errors))
    _refresh_display(state)
    _prune_orphaned_marks(state)


def _abandon_periodic_local_refresh(state: _KanpanState) -> None:
    """Drop a periodic local read in flight, so it cannot land on top of a newer refresh.

    A read still running when another refresh starts was started before it, and so holds the
    older answer. It cannot be cancelled, but its result can be left unread: the poll returns
    early once this field is clear, and the next tick starts a read that sees the change.
    """
    state.local_refresh_future = None


def _start_local_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Start a local-only background refresh (no GitHub API calls)."""
    if state.refresh_future is not None:
        return
    state.is_local_refresh_pending = False
    _abandon_periodic_local_refresh(state)
    if state.executor is None:
        state.executor = ThreadPoolExecutor(max_workers=1)
    state.refresh_is_local_only = True
    state.refresh_future = state.executor.submit(
        fetch_local_snapshot,
        state.mngr_ctx,
        state.data_sources,
        state.cached_fields,
        state.include_filters,
        state.exclude_filters,
    )
    _render_footer(state)
    _ensure_animation_running(state)
    _schedule_refresh_poll(loop, state)


def _start_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Start a full background refresh and begin the spinner animation.

    A full refresh fetches everything a local one would and starts after every request
    outstanding when it does, so starting one settles them all.
    """
    if state.refresh_future is not None:
        return
    state.is_full_refresh_pending = False
    state.is_local_refresh_pending = False
    _cancel_deferred_refresh(loop, state)
    _abandon_periodic_local_refresh(state)
    if state.executor is None:
        state.executor = ThreadPoolExecutor(max_workers=1)
    state.refresh_is_local_only = False
    state.refresh_future = state.executor.submit(
        fetch_board_snapshot,
        state.mngr_ctx,
        state.data_sources,
        state.cached_fields,
        state.include_filters,
        state.exclude_filters,
    )
    _render_footer(state)
    _ensure_animation_running(state)
    _schedule_refresh_poll(loop, state)


def _schedule_refresh_poll(loop: MainLoop, state: _KanpanState) -> None:
    """Schedule the next refresh-completion poll."""
    loop.set_alarm_in(SPINNER_INTERVAL_SECONDS, _poll_refresh_completion, state)


def _poll_refresh_completion(loop: MainLoop, state: _KanpanState) -> None:
    """Alarm callback: poll the in-flight refresh and finish it when done.

    The spinner glyph is animated by `_on_animation_tick`; this loop only watches
    for completion so the footer has a single writer.
    """
    if state.refresh_future is None:
        return

    if state.refresh_future.done():
        _finish_refresh(loop, state)
        return

    _schedule_refresh_poll(loop, state)


def _finish_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Complete a background refresh: update snapshot and display."""
    if state.refresh_future is None:
        return

    was_local_only = state.refresh_is_local_only
    failed = False
    try:
        fetch_result = state.refresh_future.result()
        new_snapshot = fetch_result.snapshot
        # Update in-memory field cache only for full refreshes: local-only refreshes do not
        # produce remote fields (PR, CI, etc.), so overwriting would lose the remote data that
        # the next full refresh needs as its cached_fields input.
        if not was_local_only:
            state.cached_fields = fetch_result.cached_fields
            save_field_cache(state.mngr_ctx, state.cached_fields)
        # For local-only refreshes, carry forward fields from previous snapshot
        if was_local_only and state.snapshot is not None:
            new_snapshot = _carry_forward_fields(state.snapshot, new_snapshot)
        if state.snapshot is not None:
            new_snapshot = _prefer_later_read(state.snapshot, new_snapshot, _LOCALLY_WRITTEN_FIELDS)
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
        else:
            # No board yet to carry an error row and no stamp to age, so the footer is the
            # only place left to say the first fetch never landed.
            state.steady_footer_text = f"  Refresh failed: {e}"
    finally:
        state.refresh_future = None
        state.refresh_is_local_only = False
        if not was_local_only:
            now = time.monotonic()
            state.last_refresh_attempt_time = now
            # Only a refresh that landed renews the stamp, so a board whose fetches are
            # failing reports the age of what it is actually showing.
            if not failed:
                state.last_successful_refresh_time = now

    _refresh_display(state)
    _prune_orphaned_marks(state)
    # A held message waits for the repaint that shows what it is describing. This refresh
    # predates the change when another is pending, so the message rides that one instead.
    if not _is_repaint_outstanding(state):
        _flush_pending_completion(state)

    if state.snapshot is not None and not was_local_only and not failed:
        state.last_fetch_seconds = state.snapshot.fetch_time_seconds
    _update_refresh_stamp(state)
    _render_footer(state)

    if failed:
        _request_refresh(loop, state, state.retry_cooldown_seconds)

    if state.is_full_refresh_pending and not failed:
        # An owed tick fetches everything a held local request would, so it stands in for it.
        # After a failure the retry sets the pace instead, and the debt rides until it starts.
        _start_refresh(loop, state)
        return

    if state.is_local_refresh_pending:
        _start_local_refresh(loop, state)


@pure
def _carry_forward_fields(old: BoardSnapshot, new: BoardSnapshot) -> BoardSnapshot:
    """Carry forward field data from a previous full snapshot for local-only refreshes.

    A local-only refresh runs every non-remote data source, so the fields those produce
    arrive fresh; the fields only remote sources produce (PR, CI, shell columns) are
    carried forward from the previous snapshot.
    """
    old_by_instance = {entry.instance_key: entry for entry in old.entries}
    updated_entries: list[AgentBoardEntry] = []
    for entry in new.entries:
        old_entry = old_by_instance.get(entry.instance_key)
        if old_entry is not None:
            # Merge: new fields override old, but keep old fields not produced by local sources
            merged_fields = dict(old_entry.fields)
            merged_fields.update(entry.fields)
            updated_entries.append(_with_fields(entry, merged_fields))
        else:
            updated_entries.append(entry)
    return BoardSnapshot(
        entries=tuple(updated_entries),
        errors=new.errors,
        fetch_time_seconds=new.fetch_time_seconds,
    )


@pure
def _prefer_later_read(old: BoardSnapshot, new: BoardSnapshot, field_keys: Sequence[str]) -> BoardSnapshot:
    """Return `new` with `field_keys` taken from whichever snapshot read them later.

    A fetch reads its values as of its start, so one still running when the board writes one
    of these holds an answer from before that write and would otherwise undo it.
    """
    old_by_instance = {entry.instance_key: entry for entry in old.entries}
    updated_entries: list[AgentBoardEntry] = []
    for entry in new.entries:
        old_entry = old_by_instance.get(entry.instance_key)
        if old_entry is None:
            updated_entries.append(entry)
            continue
        fields = dict(entry.fields)
        is_changed = False
        for key in field_keys:
            old_field = old_entry.fields.get(key)
            new_field = fields.get(key)
            if old_field is not None and new_field is not None and old_field.created > new_field.created:
                fields[key] = old_field
                is_changed = True
        updated_entries.append(_with_fields(entry, fields) if is_changed else entry)
    return new.model_copy_update(to_update(new.field_ref().entries, tuple(updated_entries)))


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


def _flatten_markup_to_attr(
    markup: str | tuple[Hashable, str] | list[str | tuple[Hashable, str]],
    attr: str,
) -> tuple[Hashable, str]:
    """Flatten rich urwid text markup to a plain string wrapped in the given attribute."""
    if isinstance(markup, list):
        plain = "".join(seg if isinstance(seg, str) else seg[1] for seg in markup)
    elif isinstance(markup, tuple):
        plain = markup[1]
    else:
        plain = markup
    return (attr, plain)


@pure
def _is_field_stale(
    field: FieldValue,
    now: datetime,
    staleness_threshold_seconds: float,
) -> bool:
    """Whether a field's `created` is older than the staleness threshold."""
    age_seconds = (now - field.created).total_seconds()
    return age_seconds > staleness_threshold_seconds


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
        return (_field_color_attr(field_key, cell.color), cell.text)
    return cell.text


class _ColumnDef(FrozenModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    header: str
    text_fn: Callable[[AgentBoardEntry], str]
    markup_fn: Callable[[AgentBoardEntry], str | tuple[Hashable, str] | list[str | tuple[Hashable, str]]]
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
        name=_NAME_COLUMN,
        header="  NAME",
        text_fn=_get_name_cell_text,
        markup_fn=_get_name_cell_markup,
        flexible=False,
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

    If column_order is None, uses DEFAULT_COLUMN_ORDER then appends any
    user-configured columns (label/shell) that are not already in the default list.
    If column_order is provided, definitions are returned in exactly that order.
    The last column always gets flexible=True.
    """
    registry: dict[str, _ColumnDef] = {d.name: d for d in builtin_defs + source_defs}
    if column_order is None:
        # Start with DEFAULT_COLUMN_ORDER, then append any extra source columns
        # (e.g. label-backed or shell columns) that aren't in the default list.
        default_set = set(DEFAULT_COLUMN_ORDER)
        extra = [d.name for d in source_defs if d.name not in default_set]
        effective_order = list(DEFAULT_COLUMN_ORDER) + extra
        result = [registry[name] for name in effective_order if name in registry]
    else:
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
def resolve_board_layout(
    data_sources: Sequence[KanpanDataSource],
    plugin_config: KanpanPluginConfig,
) -> tuple[tuple[tuple[str, str], ...], tuple[BoardSection, ...]]:
    """Resolve the board's column and section layout for non-TUI consumers.

    Returns ``(columns, section_order)`` where ``columns`` is an ordered tuple
    of ``(field_key, header)`` pairs (headers stripped of the display padding the
    TUI adds) in the same order the TUI would render them. Built from the same
    primitives ``run_kanpan`` uses (``_assemble_column_defs`` /
    ``_resolve_section_order``) so the JSON layout matches the board; ``run_kanpan``
    keeps the full ``_ColumnDef`` objects (with render closures) it needs for urwid,
    so it does not call this wrapper directly.
    """
    source_col_defs = _build_data_source_column_defs(data_sources)
    column_defs = _assemble_column_defs(_BUILTIN_COLUMN_DEFS, source_col_defs, plugin_config.column_order)
    columns = tuple((defn.name, defn.header.strip()) for defn in column_defs)
    section_order = _resolve_section_order(plugin_config.section_order)
    return columns, section_order


@pure
def _build_field_color_palette(
    snapshot: BoardSnapshot | None,
) -> tuple[list[tuple[str, str, str]], tuple[str, ...]]:
    """Build palette entries for field-based column colors.

    Scans every cell in the snapshot for colors -- both a cell's own color and
    the colors its individual runs ask for -- and creates palette entries.
    """
    entries: list[tuple[str, str, str]] = []
    attr_names: list[str] = []
    seen: set[str] = set()

    if snapshot is None:
        return entries, tuple(attr_names)

    for entry in snapshot.entries:
        for field_key, cell in entry.cells.items():
            cell_colors = [cell.color, *(run.color for run in cell.runs)]
            for color in cell_colors:
                if color is None:
                    continue
                attr = _field_color_attr(field_key, color)
                if attr not in seen:
                    seen.add(attr)
                    entries.append((attr, color, ""))
                    entries.append((f"{attr}_focus", f"{color},standout", ""))
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
    *,
    now: datetime,
    staleness_threshold_seconds: float,
) -> _SelectableRow:
    """Build a columnar urwid widget for a single agent row.

    Per-cell staleness flatten: when the field backing a column has a
    `created` older than `staleness_threshold_seconds`, that cell renders
    as ('stale', text). Whole-row muted flatten still wins over per-cell
    stale flatten -- a muted row stays uniformly grey regardless.
    """
    raw_markup: dict[str, str | tuple[Hashable, str] | list[str | tuple[Hashable, str]]] = {
        defn.name: defn.markup_fn(entry) for defn in column_defs
    }
    raw_markup[_NAME_COLUMN] = _get_name_cell_markup(entry, mark)

    # Muted agents: flatten all markup to gray
    if entry.section == BoardSection.MUTED:
        cell_markup: dict[str, str | tuple[Hashable, str] | list[str | tuple[Hashable, str]]] = {
            k: _flatten_markup_to_attr(v, "muted") for k, v in raw_markup.items()
        }
    else:
        # Per-cell stale flatten for non-muted rows
        cell_markup = {}
        for k, v in raw_markup.items():
            field = entry.fields.get(k)
            if field is not None and _is_field_stale(field, now, staleness_threshold_seconds):
                cell_markup[k] = _flatten_markup_to_attr(v, "stale")
            else:
                cell_markup[k] = v

    cols: list[tuple[int, Text | Columns] | Text | Columns] = []
    name_cell: Text | None = None
    for defn in column_defs:
        width = None if defn.flexible else widths[defn.name]
        widget = _build_cell_widget(cell_markup[defn.name], entry.cells.get(defn.name), width, defn.name)
        if defn.name == _NAME_COLUMN and isinstance(widget, Text):
            name_cell = widget
        if width is None:
            cols.append(widget)
        else:
            cols.append((width, widget))
    row = _SelectableRow(cols, dividechars=_COL_DIVIDER_CHARS)
    row.name_cell = name_cell
    return row


def _build_cell_widget(
    markup: str | tuple[Hashable, str] | list[str | tuple[Hashable, str]],
    cell: CellDisplay | None,
    width: int | None,
    field_key: str,
) -> Text | Columns:
    """Build the urwid widget for one cell of an agent row.

    A cell whose runs carry their own URLs renders as a `Columns` of one widget
    per run, so each run becomes its own terminal hyperlink and takes its own
    color; anything else is a single `Text`, hyperlinked as a whole when the
    cell carries a URL. `width` is the column's fixed width, or None for the
    flexible last column.
    """
    if cell is None:
        return Text(markup)
    if any(run.url for run in cell.runs):
        return _build_multi_link_cell(cell.runs, field_key, _cell_markup_attr(markup), width)
    if cell.url:
        hyperlink_widget = _HyperlinkText(markup)
        hyperlink_widget._hyperlink_url = cell.url
        return hyperlink_widget
    return Text(markup)


@pure
def _field_color_attr(field_key: str, color: str) -> str:
    """Palette attribute name for a color a field cell or one of its runs asked for."""
    return f"field_{field_key}_{color.replace(' ', '_')}"


@pure
def _run_color_attr(field_key: str, run: CellRun) -> str | None:
    """Palette attribute for a run's own color, or None when it takes the default."""
    if run.color is None:
        return None
    return _field_color_attr(field_key, run.color)


@pure
def _cell_markup_attr(
    markup: str | tuple[Hashable, str] | list[str | tuple[Hashable, str]],
) -> Hashable | None:
    """The single attribute a flattened field cell's markup carries, if any.

    A field cell arrives here either as plain text or as one attributed span,
    the span being the `muted` / `stale` attribute the row-level flatten
    produced. Returning it lets a multi-run cell apply that one attribute to
    every run, so a muted or stale row stays uniform.
    """
    if isinstance(markup, tuple):
        return markup[0]
    return None


def _build_multi_link_cell(
    runs: Sequence[CellRun],
    field_key: str,
    flattened_attr: Hashable | None,
    width: int | None,
) -> Columns:
    """Lay a cell's runs out side by side so each carries its own hyperlink and color.

    Every run is sized to its own visible text, so the escape bytes an
    `_HyperlinkText` injects never count towards the layout. A fixed-width
    column gets an explicit filler for whatever the runs leave over; the
    flexible column lets its last run absorb the remainder instead.

    `flattened_attr` is the row-level `muted` / `stale` attribute when one
    applies; it overrides every run's own color, so a de-emphasized row does not
    keep some runs at full brightness.
    """
    run_widths = [len(run.text) for run in runs]
    cols: list[tuple[int, Text] | Text] = []
    for idx, run in enumerate(runs):
        run_attr = flattened_attr if flattened_attr is not None else _run_color_attr(field_key, run)
        run_markup = run.text if run_attr is None else (run_attr, run.text)
        if run.url:
            hyperlink_widget = _HyperlinkText(run_markup)
            hyperlink_widget._hyperlink_url = run.url
            widget: Text = hyperlink_widget
        else:
            widget = Text(run_markup)
        is_flexible_tail = width is None and idx == len(runs) - 1
        if is_flexible_tail:
            cols.append(widget)
        else:
            cols.append((run_widths[idx], widget))
    if width is not None:
        filler_width = width - sum(run_widths)
        if filler_width > 0:
            cols.append((filler_width, Text("")))
    return Columns(cols, dividechars=0)


def _format_section_heading(section: BoardSection, count: int) -> list[str | tuple[Hashable, str]]:
    """Build urwid text markup for a section heading."""
    prefix = SECTION_PREFIX[section]
    suffix = SECTION_SUFFIX[section]
    attr = _SECTION_ATTR[section]
    if suffix:
        return [(attr, prefix), f" - {suffix} ({count})"]
    return [(attr, prefix), f" ({count})"]


@pure
def _build_focus_map(
    mark_attr_names: tuple[str, ...],
    col_attr_names: tuple[str, ...],
) -> dict[str | None, str]:
    """Attribute map that renders an agent row as the focused one."""
    focus_map: dict[str | None, str] = {None: "reversed"}
    for attr in _AGENT_LINE_ATTRS + mark_attr_names + col_attr_names:
        focus_map[attr] = f"{attr}_focus"
    return focus_map


def _build_board_widgets(
    snapshot: BoardSnapshot | None,
    column_defs: list[_ColumnDef],
    marks: dict[AgentInstanceKey, str] | None = None,
    mark_attr_names: tuple[str, ...] = (),
    col_attr_names: tuple[str, ...] = (),
    section_order: tuple[BoardSection, ...] = BOARD_SECTION_ORDER,
    staleness_threshold_seconds: float = DEFAULT_STALENESS_THRESHOLD_SECONDS,
    now: datetime | None = None,
    execute_errors: tuple[str, ...] = (),
) -> tuple[SimpleFocusListWalker[AttrMap | Text | Divider | Columns], dict[int, AgentBoardEntry]]:
    """Build the urwid widget list from a BoardSnapshot, grouped by section.

    `now` defaults to the current UTC time when None; pass an explicit value
    in tests for determinism. Reads the wall clock when `now` is None, so this
    function is intentionally not @pure.
    """
    effective_now = now if now is not None else now_utc()
    index_to_entry: dict[int, AgentBoardEntry] = {}
    walker: SimpleFocusListWalker[AttrMap | Text | Divider | Columns] = SimpleFocusListWalker([])

    if snapshot is None:
        walker.append(Text("Loading..."))
        return walker, index_to_entry

    # Compute column widths from all entries
    col_widths = _compute_board_column_widths(snapshot.entries, column_defs)

    has_content = False

    for section, entries in group_entries_by_section(snapshot, section_order):
        # Add column header before the first section
        if not has_content:
            walker.append(_build_column_header(col_widths, column_defs))
        else:
            walker.append(Divider())

        heading = _format_section_heading(section, len(entries))
        walker.append(Text(heading))
        has_content = True

        for entry in entries:
            mark = marks.get(entry.instance_key) if marks else None
            item = _build_agent_row(
                entry,
                col_widths,
                column_defs,
                mark,
                now=effective_now,
                staleness_threshold_seconds=staleness_threshold_seconds,
            )
            idx = len(walker)
            walker.append(AttrMap(item, None, focus_map=_build_focus_map(mark_attr_names, col_attr_names)))
            index_to_entry[idx] = entry

    if not has_content:
        walker.append(Text("No agents found."))

    # Show errors at the bottom: fetch/GitHub errors from the snapshot plus any
    # failures from the most recent batch execution, rendered identically.
    all_errors = (*snapshot.errors, *execute_errors)
    if all_errors:
        walker.append(Divider())
        walker.append(Text(("error_text", "Errors:")))
        for error in all_errors:
            walker.append(Text(("error_text", f"  {error}")))

    return walker, index_to_entry


def _board_body_size(state: _KanpanState) -> tuple[int, int] | None:
    """Size of the board's own rows, or None when there is no screen to measure against."""
    if state.loop is None:
        return None
    cols, rows = state.loop.screen.get_cols_rows()
    (header_rows, footer_rows), _originals = state.frame.frame_top_bottom((cols, rows), True)
    body_rows = rows - header_rows - footer_rows
    return (cols, body_rows) if cols > 0 and body_rows > 0 else None


def _focused_row_offset(state: _KanpanState, size: tuple[int, int] | None) -> int | None:
    """How far down the screen the focused row currently sits, if anything is focused."""
    body = state.frame.body
    if size is None or not isinstance(body, ListBox) or state.list_walker is None:
        return None
    _widget, position = state.list_walker.get_focus()
    if position is None:
        return None
    offset, _inset = body.get_focus_offset_inset(size)
    return offset


@pure
def _nearest_surviving_instance_key(
    previous_order: Sequence[AgentInstanceKey],
    missing: AgentInstanceKey,
    present: AbstractSet[AgentInstanceKey],
) -> AgentInstanceKey | None:
    """The agent closest to `missing` in the old board order that is still on the board.

    Searches outward from where the row used to be, preferring the one below it, which
    is where the eye already is after the row it replaced went away.
    """
    if missing not in previous_order:
        return None
    start = previous_order.index(missing)
    for distance in range(1, len(previous_order)):
        for index in (start + distance, start - distance):
            if 0 <= index < len(previous_order) and previous_order[index] in present:
                return previous_order[index]
    return None


def _refresh_display(state: _KanpanState) -> None:
    """Rebuild the body display from the current snapshot."""
    # Save the currently focused agent instance before rebuilding
    focused_entry = _get_focused_entry(state)
    if focused_entry is not None:
        state.focused_instance_key = focused_entry.instance_key

    # Where the focused row sits on screen, so the rebuild can put it back there. A fresh
    # ListBox starts scrolled to the top, and moving the walker's focus does not tell the
    # ListBox where to draw it -- so without this the view jumps on every refresh.
    body_size = _board_body_size(state)
    previous_offset = _focused_row_offset(state, body_size)
    # Board order as it stands, so a focused row that this refresh removes can hand its
    # place to whichever neighbour survived rather than dropping the anchor entirely.
    previous_order = tuple(entry.instance_key for _index, entry in sorted(state.index_to_entry.items()))

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
        staleness_threshold_seconds=state.staleness_threshold_seconds,
        execute_errors=state.execute_errors,
    )
    state.list_walker = walker
    listbox: ListBox = ListBox(walker)
    state.frame.body = listbox

    # Restore focus to the previously focused agent, at the height it was already at.
    if state.focused_instance_key is not None:
        restored_index = _focus_row_by_instance_key(state, state.focused_instance_key)
        if restored_index is None and state.search_input is None:
            # The focused row is gone -- deleted, or filtered away. Anchoring on its
            # nearest surviving neighbour keeps the board where the eye left it; with no
            # anchor at all a fresh ListBox renders from the top. An open search anchors
            # itself, back to the row it started from, so it is left to do that.
            replacement = _nearest_surviving_instance_key(
                previous_order,
                state.focused_instance_key,
                {entry.instance_key for entry in state.index_to_entry.values()},
            )
            if replacement is not None:
                state.focused_instance_key = replacement
                restored_index = _focus_row_by_instance_key(state, replacement)
        if restored_index is not None and previous_offset is not None and body_size is not None:
            listbox.change_focus(body_size, restored_index, offset_inset=previous_offset)

    # An open peek panel's title shows live state; re-render it from the new entries.
    _update_peek_header(state)

    _render_header_status(state)

    # Every row was just rebuilt, so an open search re-ranks against the new board,
    # keeping the user on the match they cycled to for as long as it still matches.
    if state.search_input is not None:
        current_match = state.search_matches[state.search_index] if state.search_matches else None
        _apply_search_query(state, state.search_input.get_edit_text(), keep_match=current_match)


def _schedule_next_refresh(loop: MainLoop, state: _KanpanState) -> None:
    """Schedule the next auto-refresh alarm."""
    loop.set_alarm_in(state.refresh_interval_seconds, _on_auto_refresh_alarm, state)


def _on_auto_refresh_alarm(loop: MainLoop, state: _KanpanState) -> None:
    """Alarm callback for the periodic full refresh.

    A firing that lands while a full refresh is already running is skipped: the interval says
    how often to start one, not how many to have going. A local-only refresh in the slot is
    the weaker read -- it renews neither the remote columns nor the stamp -- so the tick it
    displaces is owed rather than dropped, and runs as soon as that read finishes.

    Re-arming here rather than on completion is what makes the interval a period and keeps
    the chain alive down every path a refresh can take, including the ones that never reach
    completion.
    """
    if state.refresh_future is None:
        _start_refresh(loop, state)
    else:
        # The tick is owed exactly when the slot holds the weaker read: a full refresh
        # already covers it, a local-only one cannot.
        state.is_full_refresh_pending = state.refresh_is_local_only
    _schedule_next_refresh(loop, state)


def _load_user_commands(mngr_ctx: MngrContext) -> dict[str, CustomCommand]:
    """Load user-defined commands from plugin config.

    Values may arrive as either `CustomCommand` instances (when the caller
    constructed the config directly) or raw dicts (when the TOML loader used
    `model_construct`, which bypasses Pydantic's recursive validation and
    leaves nested dict-typed fields in their raw form).
    """
    config = mngr_ctx.get_plugin_config("kanpan", KanpanPluginConfig)
    result: dict[str, CustomCommand] = {}
    for key, value in config.commands.items():
        if isinstance(value, CustomCommand):
            result[key] = value
        elif isinstance(value, dict):
            result[key] = CustomCommand(**value)
    return result


def _shutdown_executors(state: _KanpanState) -> None:
    """Release every worker pool the board opened, without waiting on work still running.

    A whole batch is submitted at once, so that queue can be deep. Its unstarted work is
    cancelled: the interpreter joins thread-pool workers at exit, so anything left queued
    would hold the process open until each abandoned operation had run.
    """
    if state.executor is not None:
        state.executor.shutdown(wait=False)
    if state.local_refresh_executor is not None:
        state.local_refresh_executor.shutdown(wait=False)
    if state.peek_executor is not None:
        state.peek_executor.shutdown(wait=False)
    if state.peek_reply_executor is not None:
        state.peek_reply_executor.shutdown(wait=False)
    if state.prompt_executor is not None:
        state.prompt_executor.shutdown(wait=False)
    if state.batch_executor is not None:
        state.batch_executor.shutdown(wait=False, cancel_futures=True)


def _build_command_map(mngr_ctx: MngrContext) -> dict[str, KanpanCommand]:
    """Build the unified command map: builtins merged with user config."""
    commands: dict[str, KanpanCommand] = dict(_BUILTIN_COMMANDS)
    user_commands = _load_user_commands(mngr_ctx)
    commands.update(user_commands)
    return {key: cmd for key, cmd in commands.items() if cmd.enabled}


@pure
def _build_mark_palette(
    commands: dict[str, KanpanCommand],
) -> tuple[list[tuple[str, str, str]], tuple[str, ...]]:
    """Build palette entries and attr names for markable commands."""
    entries: list[tuple[str, str, str]] = []
    attr_names: list[str] = []
    for key, cmd in commands.items():
        color = _mark_color(cmd)
        if color is None:
            continue
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
    # Compiled before the screen is taken, so a misconfigured template reports
    # itself on the terminal rather than under the board.
    header_status = compile_header_status(plugin_config.header_status)
    plugin_config.check_refresh_intervals()

    # Collect data sources and load cached fields from disk
    data_sources = collect_data_sources(mngr_ctx)
    initial_cached_fields = load_field_cache(mngr_ctx, data_sources)

    legend_bindings, footer_legend = _build_legend_bindings(commands)

    footer_left_text = Text("  Loading...")
    footer_left_attr = AttrMap(footer_left_text, "footer")
    # The legend is filled by _set_footer_legend once the state exists, so the belt
    # is only ever written by the one path that fits it to the available width.
    footer, footer_belt, footer_right = _build_footer(footer_left_attr)

    is_filtered = bool(include_filters or exclude_filters)
    header_title = HEADER_TITLE
    if is_filtered:
        header_title += "  [filtered]"
    header, header_status_text = _build_header(header_title)

    initial_body = Filler(Pile([Text("Loading...")]), valign="top")
    frame = _BoardFrame(body=initial_body, header=header, footer=footer)

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
        local_refresh_interval_seconds=plugin_config.local_refresh_interval_seconds,
        retry_cooldown_seconds=plugin_config.retry_cooldown_seconds,
        batch_concurrency=plugin_config.batch_concurrency,
        staleness_threshold_seconds=plugin_config.effective_staleness_threshold_seconds(),
        mark_attr_names=mark_attr_names,
        column_defs=column_defs,
        data_sources=data_sources,
        cached_fields=initial_cached_fields,
        include_filters=include_filters,
        exclude_filters=exclude_filters,
        section_order=section_order,
        legend_bindings=legend_bindings,
        header_status=header_status,
        header_status_text=header_status_text,
        footer_legend=footer_legend,
        footer_pile=footer,
        footer_columns=footer_belt,
    )

    frame.kanpan_state = state
    _set_footer_legend(state, footer_legend)

    input_handler = _KanpanInputHandler(state=state)

    with create_urwid_screen_preserving_terminal() as screen:
        loop = MainLoop(
            frame,
            palette=PALETTE + mark_palette_entries,
            unhandled_input=input_handler,
            # urwid annotates input_filter as taking list[str] but delivers mouse
            # events to it as tuples, the way it does for unhandled_input.
            input_filter=_KanpanInputFilter(state=state),  # ty: ignore[invalid-argument-type]
            screen=screen,
        )
        state.loop = loop

        # Initial data load with spinner
        _start_refresh(loop, state)
        loop.set_alarm_in(_STAMP_TICK_SECONDS, _on_stamp_tick, state)
        # Each periodic refresh runs off a chain its own alarm keeps going, so both start here.
        _schedule_next_refresh(loop, state)
        _schedule_next_local_refresh(loop, state)

        screen.write(_TITLE_STACK_PUSH)
        _write_terminal_title(screen, TERMINAL_TITLE)
        logger.disable("imbue")
        try:
            loop.run()
        finally:
            screen.write(_TITLE_STACK_POP)
            screen.flush()
            logger.enable("imbue")
            _shutdown_executors(state)
