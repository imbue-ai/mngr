"""Unit tests for the kanpan TUI."""

import io
import re
import subprocess
import threading
from collections.abc import Sequence
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError
from urwid.display.raw import Screen
from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.widget.attr_map import AttrMap
from urwid.widget.filler import Filler
from urwid.widget.frame import Frame
from urwid.widget.listbox import ListBox
from urwid.widget.pile import Pile
from urwid.widget.text import Text

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FIELD_PR
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_sources.git_info import CommitsAheadField
from imbue.mngr_kanpan.data_sources.github import CiField
from imbue.mngr_kanpan.data_sources.github import CiStatus
from imbue.mngr_kanpan.data_sources.github import PrField
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_sources.labels import LabelColumnConfig
from imbue.mngr_kanpan.data_sources.labels import LabelsDataSource
from imbue.mngr_kanpan.data_types import ActionBuiltinCommand
from imbue.mngr_kanpan.data_types import ActionBuiltinRole
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import CustomCommand
from imbue.mngr_kanpan.data_types import KanpanCommand
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.data_types import MarkableBuiltinCommand
from imbue.mngr_kanpan.data_types import MarkableBuiltinRole
from imbue.mngr_kanpan.header_status import compile_header_status
from imbue.mngr_kanpan.testing import make_additional_pr
from imbue.mngr_kanpan.testing import make_agent_details
from imbue.mngr_kanpan.testing import make_board_entry
from imbue.mngr_kanpan.testing import make_board_snapshot
from imbue.mngr_kanpan.testing import make_mngr_ctx
from imbue.mngr_kanpan.testing import make_mngr_ctx_with_config
from imbue.mngr_kanpan.testing import make_pr_field
from imbue.mngr_kanpan.tui import BOARD_SECTION_ORDER
from imbue.mngr_kanpan.tui import HEADER_TITLE
from imbue.mngr_kanpan.tui import PEEK_BODY_HEIGHT
from imbue.mngr_kanpan.tui import _BUILTIN_COLUMN_DEFS
from imbue.mngr_kanpan.tui import _BUILTIN_COMMANDS
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_DELETE
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_EXECUTE
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_MUTE
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_PUSH
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_REFRESH
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_SEARCH
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_UNMARK
from imbue.mngr_kanpan.tui import _BatchItemResult
from imbue.mngr_kanpan.tui import _BatchWorkItem
from imbue.mngr_kanpan.tui import _BoardFrame
from imbue.mngr_kanpan.tui import _ColumnDef
from imbue.mngr_kanpan.tui import _FOOTER_STATUS_SLOT
from imbue.mngr_kanpan.tui import _FieldCellMarkupFn
from imbue.mngr_kanpan.tui import _FieldCellTextFn
from imbue.mngr_kanpan.tui import _KanpanInputFilter
from imbue.mngr_kanpan.tui import _KanpanInputHandler
from imbue.mngr_kanpan.tui import _KanpanState
from imbue.mngr_kanpan.tui import _LEGEND_SEPARATOR
from imbue.mngr_kanpan.tui import _SEARCH_QUERY_MIN_COLS
from imbue.mngr_kanpan.tui import _SelectableRow
from imbue.mngr_kanpan.tui import _assemble_column_defs
from imbue.mngr_kanpan.tui import _batch_item_label
from imbue.mngr_kanpan.tui import _binding_description
from imbue.mngr_kanpan.tui import _board_body_size
from imbue.mngr_kanpan.tui import _build_agent_row
from imbue.mngr_kanpan.tui import _build_board_widgets
from imbue.mngr_kanpan.tui import _build_command_map
from imbue.mngr_kanpan.tui import _build_data_source_column_defs
from imbue.mngr_kanpan.tui import _build_field_color_palette
from imbue.mngr_kanpan.tui import _build_focus_map
from imbue.mngr_kanpan.tui import _build_footer
from imbue.mngr_kanpan.tui import _build_header
from imbue.mngr_kanpan.tui import _build_legend_bindings
from imbue.mngr_kanpan.tui import _build_mark_palette
from imbue.mngr_kanpan.tui import _build_peek_panel
from imbue.mngr_kanpan.tui import _cancel_peek_alarm
from imbue.mngr_kanpan.tui import _carry_forward_fields
from imbue.mngr_kanpan.tui import _clear_focus
from imbue.mngr_kanpan.tui import _close_peek
from imbue.mngr_kanpan.tui import _close_prompt
from imbue.mngr_kanpan.tui import _close_search
from imbue.mngr_kanpan.tui import _compute_board_column_widths
from imbue.mngr_kanpan.tui import _compute_footer_display
from imbue.mngr_kanpan.tui import _cycle_search
from imbue.mngr_kanpan.tui import _dispatch_command
from imbue.mngr_kanpan.tui import _ensure_batch_executor
from imbue.mngr_kanpan.tui import _ensure_peek_executor
from imbue.mngr_kanpan.tui import _ensure_peek_reply_executor
from imbue.mngr_kanpan.tui import _ensure_prompt_executor
from imbue.mngr_kanpan.tui import _execute_batch
from imbue.mngr_kanpan.tui import _execute_marks
from imbue.mngr_kanpan.tui import _field_cell_markup
from imbue.mngr_kanpan.tui import _field_cell_text
from imbue.mngr_kanpan.tui import _find_entry_by_name
from imbue.mngr_kanpan.tui import _finish_batch_execution
from imbue.mngr_kanpan.tui import _finish_refresh
from imbue.mngr_kanpan.tui import _fit_legend
from imbue.mngr_kanpan.tui import _flatten_markup_to_attr
from imbue.mngr_kanpan.tui import _flush_pending_completion
from imbue.mngr_kanpan.tui import _focus_row_by_name
from imbue.mngr_kanpan.tui import _format_section_heading
from imbue.mngr_kanpan.tui import _get_focused_entry
from imbue.mngr_kanpan.tui import _get_name_cell_markup
from imbue.mngr_kanpan.tui import _get_state_attr
from imbue.mngr_kanpan.tui import _handle_peek_key
from imbue.mngr_kanpan.tui import _handle_prompt_key
from imbue.mngr_kanpan.tui import _handle_search_key
from imbue.mngr_kanpan.tui import _is_field_stale
from imbue.mngr_kanpan.tui import _is_focus_on_first_selectable
from imbue.mngr_kanpan.tui import _is_modal_surface_open
from imbue.mngr_kanpan.tui import _is_transcript_header
from imbue.mngr_kanpan.tui import _last_nonempty_line
from imbue.mngr_kanpan.tui import _legend_markup
from imbue.mngr_kanpan.tui import _legend_width
from imbue.mngr_kanpan.tui import _load_user_commands
from imbue.mngr_kanpan.tui import _make_readline_edit
from imbue.mngr_kanpan.tui import _mark_color
from imbue.mngr_kanpan.tui import _nearest_surviving_name
from imbue.mngr_kanpan.tui import _on_batch_poll
from imbue.mngr_kanpan.tui import _on_peek_capture_poll
from imbue.mngr_kanpan.tui import _on_peek_reply_poll
from imbue.mngr_kanpan.tui import _on_stamp_tick
from imbue.mngr_kanpan.tui import _on_transient_expire
from imbue.mngr_kanpan.tui import _open_prompt
from imbue.mngr_kanpan.tui import _open_prompt_for_command
from imbue.mngr_kanpan.tui import _open_search
from imbue.mngr_kanpan.tui import _packed_width
from imbue.mngr_kanpan.tui import _peek_body_lines
from imbue.mngr_kanpan.tui import _peek_body_markup
from imbue.mngr_kanpan.tui import _prompted_mark_keys
from imbue.mngr_kanpan.tui import _prune_orphaned_marks
from imbue.mngr_kanpan.tui import _rank_matches
from imbue.mngr_kanpan.tui import _record_batch_result
from imbue.mngr_kanpan.tui import _refresh_display
from imbue.mngr_kanpan.tui import _refresh_stamp
from imbue.mngr_kanpan.tui import _render_footer
from imbue.mngr_kanpan.tui import _render_header_status
from imbue.mngr_kanpan.tui import _report_after_refresh
from imbue.mngr_kanpan.tui import _resolve_section_order
from imbue.mngr_kanpan.tui import _run_shell_command
from imbue.mngr_kanpan.tui import _run_shell_command_sync
from imbue.mngr_kanpan.tui import _search_counter_text
from imbue.mngr_kanpan.tui import _search_rows
from imbue.mngr_kanpan.tui import _set_footer_legend
from imbue.mngr_kanpan.tui import _short_header
from imbue.mngr_kanpan.tui import _show_transient_message
from imbue.mngr_kanpan.tui import _shutdown_executors
from imbue.mngr_kanpan.tui import _submit_batch_item
from imbue.mngr_kanpan.tui import _submit_peek_reply
from imbue.mngr_kanpan.tui import _submit_prompt
from imbue.mngr_kanpan.tui import _toggle_mark
from imbue.mngr_kanpan.tui import _toggle_peek
from imbue.mngr_kanpan.tui import _unmark_all
from imbue.mngr_kanpan.tui import _unmark_focused
from imbue.mngr_kanpan.tui import _update_mark_count_footer
from imbue.mngr_kanpan.tui import _update_peek_header
from imbue.mngr_kanpan.tui import _update_refresh_stamp
from imbue.mngr_kanpan.tui import _update_row_mark
from imbue.mngr_kanpan.tui import _update_snapshot_mute
from imbue.mngr_kanpan.tui import _write_terminal_title
from imbue.mngr_kanpan.tui import resolve_board_layout

# =============================================================================
# Helpers
# =============================================================================


class _CallTracker:
    """Lightweight call tracker."""

    def __init__(self) -> None:
        self.call_count: int = 0

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.call_count += 1


_TEST_SCREEN_COLS: int = 120
_TEST_SCREEN_ROWS: int = 24
# Narrow enough that the belt leaves the search prompt a slot the test's query overruns.
_NARROW_SCREEN_COLS: int = 80


class _MockScreen:
    """Stand-in for the urwid screen, which the footer measures itself against.

    ``cols`` is writable so a test can stand a terminal resize up.
    """

    def __init__(self, cols: int = _TEST_SCREEN_COLS, rows: int = _TEST_SCREEN_ROWS) -> None:
        self.cols = cols
        self.rows = rows

    def get_cols_rows(self) -> tuple[int, int]:
        return (self.cols, self.rows)


def _make_mock_loop() -> Any:
    tracker = _CallTracker()
    return SimpleNamespace(set_alarm_in=tracker, _alarm_tracker=tracker, screen=_MockScreen())


def _make_entry_from_fields(name: str, fields: dict[str, FieldValue]) -> AgentBoardEntry:
    """An entry whose cells are rendered from its fields, the way the fetcher builds one."""
    return make_board_entry(name=name, fields=fields, cells={key: field.display() for key, field in fields.items()})


def _make_state(
    snapshot: BoardSnapshot | None = None,
    commands: dict[str, CustomCommand] | None = None,
) -> _KanpanState:
    footer_left_text = Text("  Loading...")
    footer_left_attr = AttrMap(footer_left_text, "footer")
    footer_right = Text("")
    frame = Frame(body=Filler(Text("")))
    mock_ctx = SimpleNamespace(get_plugin_config=lambda name, cls: cls())
    return _KanpanState.model_construct(
        mngr_ctx=mock_ctx,
        snapshot=snapshot,
        frame=frame,
        footer_left_text=footer_left_text,
        footer_left_attr=footer_left_attr,
        footer_right=footer_right,
        header_status_text=Text(""),
        commands=commands or {},
        column_defs=list(_BUILTIN_COLUMN_DEFS),
        marks={},
        executing=False,
        index_to_entry={},
        list_walker=None,
        focused_agent_name=None,
        steady_footer_text="  Loading...",
        last_refresh_time=0.0,
        refresh_is_local_only=False,
        deferred_refresh_alarm=None,
        deferred_refresh_fire_at=0.0,
        refresh_interval_seconds=600.0,
        retry_cooldown_seconds=60.0,
        mark_attr_names=(),
        col_attr_names=(),
        data_sources=(),
        include_filters=(),
        exclude_filters=(),
        spinner_index=0,
        refresh_future=None,
        executor=None,
        loop=None,
    )


# =============================================================================
# State attr / name markup
# =============================================================================


def test_get_state_attr_running() -> None:
    entry = make_board_entry(state=AgentLifecycleState.RUNNING)
    assert _get_state_attr(entry) == "state_running"


def test_get_state_attr_waiting() -> None:
    entry = make_board_entry(state=AgentLifecycleState.WAITING)
    assert _get_state_attr(entry) == "state_attention"


def test_get_state_attr_done() -> None:
    entry = make_board_entry(state=AgentLifecycleState.DONE)
    assert _get_state_attr(entry) == ""


def test_get_name_cell_markup_no_mark() -> None:
    entry = make_board_entry()
    markup = _get_name_cell_markup(entry)
    assert markup == "  test-agent"


def test_get_name_cell_markup_with_mark() -> None:
    entry = make_board_entry()
    markup = _get_name_cell_markup(entry, mark_key="d")
    assert isinstance(markup, list)
    assert ("mark_d", "d") in markup


# =============================================================================
# Section headings
# =============================================================================


def test_format_section_heading_with_suffix() -> None:
    heading = _format_section_heading(BoardSection.PR_MERGED, 3)
    assert len(heading) == 2
    assert heading[0] == ("section_done", "Done")
    assert "3" in heading[1]


def test_format_section_heading_muted_no_suffix() -> None:
    heading = _format_section_heading(BoardSection.MUTED, 1)
    assert heading[0] == ("section_muted", "Muted")
    assert "(1)" in heading[1]


# =============================================================================
# Board widgets
# =============================================================================


def test_build_board_widgets_none_snapshot() -> None:
    walker, idx_map = _build_board_widgets(None, _BUILTIN_COLUMN_DEFS)
    assert len(walker) == 1
    assert idx_map == {}


def test_build_board_widgets_empty_entries() -> None:
    snapshot = make_board_snapshot(entries=())
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert idx_map == {}


def test_build_board_widgets_one_entry() -> None:
    entry = make_board_entry(section=BoardSection.STILL_COOKING)
    snapshot = make_board_snapshot(entries=(entry,))
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert len(idx_map) == 1


def test_build_board_widgets_errors_displayed() -> None:
    snapshot = make_board_snapshot(entries=(), errors=("Error 1",))
    walker, _ = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    texts = [w.text if hasattr(w, "text") else "" for w in walker]
    found_error = any("Error 1" in str(t) for t in texts)
    assert found_error


def test_build_board_widgets_execute_errors_displayed() -> None:
    snapshot = make_board_snapshot(entries=())
    walker, _ = _build_board_widgets(
        snapshot, _BUILTIN_COLUMN_DEFS, execute_errors=("delete foo: timed out after 60s",)
    )
    texts = [str(w.text) if hasattr(w, "text") else "" for w in walker]
    assert any("delete foo: timed out after 60s" in t for t in texts)


def test_build_board_widgets_execute_and_fetch_errors_share_one_block() -> None:
    snapshot = make_board_snapshot(entries=(), errors=("fetch boom",))
    walker, _ = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS, execute_errors=("delete bar: failed",))
    texts = [str(w.text) if hasattr(w, "text") else "" for w in walker]
    # A single "Errors:" header covers both fetch and execution errors.
    assert sum(1 for t in texts if t.strip() == "Errors:") == 1
    assert any("fetch boom" in t for t in texts)
    assert any("delete bar: failed" in t for t in texts)


def test_build_board_widgets_groups_by_section() -> None:
    e1 = make_board_entry(name="a", section=BoardSection.STILL_COOKING)
    e2 = make_board_entry(name="b", section=BoardSection.PR_MERGED)
    snapshot = make_board_snapshot(entries=(e1, e2))
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert len(idx_map) == 2


# =============================================================================
# Column assembly
# =============================================================================


def test_assemble_column_defs_no_order_no_custom() -> None:
    result = _assemble_column_defs(_BUILTIN_COLUMN_DEFS, [], None)
    # With no source defs, only builtin columns that appear in DEFAULT_COLUMN_ORDER are included
    assert len(result) == len(_BUILTIN_COLUMN_DEFS)
    assert result[-1].flexible is True


def test_assemble_column_defs_with_order() -> None:
    result = _assemble_column_defs(_BUILTIN_COLUMN_DEFS, [], ["state", "name"])
    assert len(result) == 2
    assert result[0].name == "state"
    assert result[1].name == "name"
    assert result[-1].flexible is True


def test_assemble_column_defs_unknown_names_skipped() -> None:
    result = _assemble_column_defs(_BUILTIN_COLUMN_DEFS, [], ["name", "nonexistent"])
    assert len(result) == 1
    assert result[0].name == "name"


def test_assemble_column_defs_default_order_appends_extras() -> None:
    """Extra source columns not in DEFAULT_COLUMN_ORDER are appended at the end."""
    extra_def = _ColumnDef(
        name="slack_thread",
        header="SLACK",
        text_fn=_FieldCellTextFn(field_key="slack_thread"),
        markup_fn=_FieldCellMarkupFn(field_key="slack_thread"),
        flexible=False,
    )
    result = _assemble_column_defs(_BUILTIN_COLUMN_DEFS, [extra_def], None)
    names = [d.name for d in result]
    # Builtins from DEFAULT_COLUMN_ORDER come first, then extras
    assert names[0] == "name"
    assert names[1] == "state"
    assert "slack_thread" in names
    assert names[-1] == "slack_thread"
    assert result[-1].flexible is True


def test_assemble_column_defs_default_order_includes_default_columns() -> None:
    """When source defs include columns from DEFAULT_COLUMN_ORDER, they appear in default order."""
    pr_def = _ColumnDef(
        name="pr",
        header="PR",
        text_fn=_FieldCellTextFn(field_key="pr"),
        markup_fn=_FieldCellMarkupFn(field_key="pr"),
        flexible=False,
    )
    ci_def = _ColumnDef(
        name="ci",
        header="CI",
        text_fn=_FieldCellTextFn(field_key="ci"),
        markup_fn=_FieldCellMarkupFn(field_key="ci"),
        flexible=False,
    )
    result = _assemble_column_defs(_BUILTIN_COLUMN_DEFS, [pr_def, ci_def], None)
    names = [d.name for d in result]
    # Should follow DEFAULT_COLUMN_ORDER: name, state, ..., pr, ci, ...
    pr_idx = names.index("pr")
    ci_idx = names.index("ci")
    assert pr_idx < ci_idx


# =============================================================================
# Mark palette
# =============================================================================


def test_build_mark_palette_no_markable() -> None:
    commands: dict[str, KanpanCommand] = {"r": CustomCommand(name="refresh")}
    entries, names = _build_mark_palette(commands)
    assert entries == []
    assert names == ()


def test_build_mark_palette_markable() -> None:
    commands: dict[str, KanpanCommand] = {"d": CustomCommand(name="delete", markable="light red")}
    entries, names = _build_mark_palette(commands)
    assert len(entries) == 2
    assert "mark_d" in names


def test_custom_command_is_markable_agrees_with_mark_color() -> None:
    # `CustomCommand.is_markable` gates the prompt/markable rejection, while
    # `_mark_color(cmd) is not None` gates dispatch. They must answer identically for
    # every `markable` shape, or a rejected pair slips through and swallows the prompt.
    for markable in (False, True, "", "light red"):
        cmd = CustomCommand(name="tag", command="true", markable=markable)
        assert cmd.is_markable is (_mark_color(cmd) is not None)


# =============================================================================
# State management
# =============================================================================


def test_show_transient_message() -> None:
    state = _make_state()
    state.loop = _make_mock_loop()
    _show_transient_message(state, "  Test message")
    assert state.footer_left_text.text == "  Test message"


def test_transient_message_expires_to_steady() -> None:
    state = _make_state()
    state.loop = _make_mock_loop()
    state.steady_footer_text = "  Steady"
    _show_transient_message(state, "  Test message")
    assert state.footer_left_text.text == "  Test message"
    _on_transient_expire(state.loop, state)
    assert state.transient_message is None
    assert state.footer_left_text.text == "  Steady"


class _RecordingLoop:
    """Mock loop that hands out real alarm handles and records cancellations.

    Lets us exercise the transient-message debounce, where a second message must
    cancel the first message's pending expiry alarm so it cannot clear the new one.
    """

    def __init__(self) -> None:
        self.next_handle = 0
        self.removed: list[int] = []
        self.screen = _MockScreen()

    def set_alarm_in(self, _seconds: float, _callback: Any, _data: Any = None) -> int:
        handle = self.next_handle
        self.next_handle += 1
        return handle

    def remove_alarm(self, handle: int) -> None:
        self.removed.append(handle)


def test_show_transient_message_cancels_previous_alarm() -> None:
    state = _make_state()
    loop = _RecordingLoop()
    state.loop = cast(Any, loop)
    _show_transient_message(state, "  First")
    first_handle = state.transient_alarm
    _show_transient_message(state, "  Second")
    # The first message's expiry alarm was cancelled so it cannot clear the second.
    assert loop.removed == [first_handle]
    assert state.transient_alarm != first_handle
    assert state.footer_left_text.text == "  Second"


def test_footer_priority_action_wins_over_refresh() -> None:
    # Regression: a refresh and a user action (e.g. delete) overlapping must not
    # flicker. The single-owner footer shows the action label, not "Refreshing".
    state = _make_state()
    # A stand-in object for an in-flight refresh future.
    state.refresh_future = cast(Any, object())
    state.action_label = "  [1/1] delete agent-a"
    text, attr = _compute_footer_display(state)
    assert text.startswith("  [1/1] delete agent-a")
    assert "Refreshing" not in text
    assert attr == "footer"


def test_footer_priority_refresh_when_no_action() -> None:
    state = _make_state()
    state.refresh_future = cast(Any, object())
    text, _ = _compute_footer_display(state)
    assert text.startswith("  Refreshing")


def test_footer_transient_overrides_action_and_refresh() -> None:
    state = _make_state()
    state.refresh_future = cast(Any, object())
    state.action_label = "  [1/1] delete agent-a"
    state.transient_message = "  Done"
    text, attr = _compute_footer_display(state)
    assert text == "  Done"
    assert attr == "notification"


def test_render_footer_is_single_writer_no_flicker() -> None:
    # Both the refresh poll context and the action context render through the same
    # function; the displayed text stays the action label across repeated renders.
    state = _make_state()
    state.refresh_future = cast(Any, object())
    state.action_label = "  Running deploy on agent-a"
    _render_footer(state)
    first = state.footer_left_text.text
    state.spinner_index += 1
    _render_footer(state)
    second = state.footer_left_text.text
    assert first.startswith("  Running deploy on agent-a")
    assert second.startswith("  Running deploy on agent-a")


# =============================================================================
# Header
# =============================================================================


def _render_header_lines(header: Pile, cols: int) -> list[str]:
    """Render `header`'s first row at `cols` columns, as one string per screen line."""
    return [line.decode() for line in header.contents[0][0].render((cols,)).text]


def _render_header_rows(title: str, status: str, cols: int) -> list[str]:
    """Render a header built for `title` and `status`, as one string per screen line."""
    header, status_text = _build_header(title)
    status_text.set_text(status)
    return _render_header_lines(header, cols)


def _render_header_row(title: str, status: str, cols: int) -> str:
    """Render the header's first row at `cols` columns and return it as one line."""
    return "".join(_render_header_rows(title, status, cols))


@pytest.mark.parametrize("status", ["", "3 running / 45  ", "137 running / 402  "])
def test_build_header_centres_the_title_whatever_the_status(status: str) -> None:
    cols = 100
    line = _render_header_row(HEADER_TITLE, status, cols)
    start = line.index(HEADER_TITLE)
    # The title's width in screen columns, which its double-width 看 puts above its
    # character count.
    title_width = Text(HEADER_TITLE).pack()[0]
    # The title's own centre lands on the screen's centre, to within the half column
    # an unequal split has to give to one side.
    assert abs((start + start + title_width) / 2 - cols / 2) <= 0.5


def test_build_header_shows_the_status_at_the_right_edge() -> None:
    line = _render_header_row(HEADER_TITLE, "3 running / 45  ", 100)
    assert line.rstrip().endswith("3 running / 45")


def test_build_header_hides_a_status_too_wide_to_fit_whole() -> None:
    # A right-aligned clip would drop the leading characters, so a status that
    # does not fit is dropped whole rather than shown as a fragment of itself.
    status = "137 running · 12 failing CI · 40 in review  "
    line = _render_header_row(HEADER_TITLE, status, 80)
    assert line.strip() == HEADER_TITLE


@pytest.mark.parametrize("cols", [19, 16, 15, 13, 12, 10])
def test_build_header_keeps_the_title_on_a_narrow_terminal(cols: int) -> None:
    # A width urwid reserves for the cells either side of the title is a width it
    # takes from the title itself, and it drops a column it cannot fit: the title
    # must wrap on a narrow terminal, not vanish.
    rows = _render_header_rows(HEADER_TITLE, "3 running / 45  ", cols)
    assert " ".join(rows).split() == HEADER_TITLE.split()


def test_render_header_status_writes_the_counts() -> None:
    state = _make_state(
        snapshot=make_board_snapshot(
            entries=(
                make_board_entry(name="a", state=AgentLifecycleState.RUNNING),
                make_board_entry(name="b", state=AgentLifecycleState.STOPPED),
            )
        )
    )
    state.header_status = compile_header_status('{state == "RUNNING"} running / {total}')
    _render_header_status(state)
    assert state.header_status_text.text.strip() == "1 running / 2"


def test_render_header_status_keeps_a_gap_from_the_title() -> None:
    # The status cell starts in the column right after the packed title, so at the
    # widths where the status has just enough room to be shown at all, the padding
    # the writer adds is the only thing holding the two apart.
    shown_status = "1 agents"
    header, status_text = _build_header(HEADER_TITLE)
    state = _make_state(snapshot=make_board_snapshot(entries=(make_board_entry(name="a"),)))
    state.header_status = compile_header_status("{total} agents")
    state.header_status_text = status_text
    _render_header_status(state)

    widths_showing_the_status: list[int] = []
    for cols in range(60, 120):
        line = "".join(_render_header_lines(header, cols))
        if shown_status not in line:
            continue
        widths_showing_the_status.append(cols)
        assert line.split(shown_status)[0].endswith("  "), f"status abuts the title at {cols} columns: {line!r}"
    assert widths_showing_the_status != []


def test_render_header_status_unconfigured_stays_empty() -> None:
    state = _make_state(snapshot=make_board_snapshot(entries=(make_board_entry(),)))
    state.header_status_text = Text("unexpected")
    _render_header_status(state)
    assert state.header_status_text.text == ""


def test_update_snapshot_mute() -> None:
    entry = make_board_entry(is_muted=False)
    state = _make_state(snapshot=make_board_snapshot(entries=(entry,)))
    _update_snapshot_mute(state, AgentName("test-agent"), True)
    assert state.snapshot is not None
    assert state.snapshot.entries[0].is_muted is True


def test_prune_orphaned_marks() -> None:
    entry = make_board_entry(name="agent-a")
    state = _make_state(snapshot=make_board_snapshot(entries=(entry,)))
    state.marks = {AgentName("agent-a"): "d", AgentName("agent-b"): "d"}
    _prune_orphaned_marks(state)
    assert AgentName("agent-a") in state.marks
    assert AgentName("agent-b") not in state.marks


def test_clear_focus() -> None:
    state = _make_state()
    state.focused_agent_name = AgentName("test")
    _clear_focus(state)
    assert state.focused_agent_name is None


# =============================================================================
# Batch items
# =============================================================================


def test_batch_item_label_single() -> None:
    item = _BatchWorkItem(
        name=AgentName("agent-1"),
        key="p",
        cmd=CustomCommand(name="push"),
        entry=None,
    )
    assert _batch_item_label(item) == "push agent-1"


def test_batch_item_label_batch() -> None:
    item = _BatchWorkItem(
        name=AgentName("agent-1"),
        key="d",
        cmd=CustomCommand(name="delete"),
        entry=None,
        batch_names=(AgentName("agent-1"), AgentName("agent-2")),
    )
    assert "2 agent(s)" in _batch_item_label(item)


# =============================================================================
# Input handler
# =============================================================================


def test_input_handler_quit() -> None:
    state = _make_state()
    handler = _KanpanInputHandler(state=state)
    with pytest.raises(ExitMainLoop):
        handler("q")


def test_input_handler_tuple_passthrough() -> None:
    state = _make_state()
    handler = _KanpanInputHandler(state=state)
    assert handler(("mouse press", 1, 0, 0)) is None


def test_input_handler_unknown_key_consumed() -> None:
    state = _make_state()
    handler = _KanpanInputHandler(state=state)
    assert handler("z") is True


# =============================================================================
# Field-based rendering
# =============================================================================


def test_field_cell_text_present() -> None:
    entry = make_board_entry(cells={"ci": CellDisplay(text="failure", color="light red")})
    assert _field_cell_text(entry, "ci") == "failure"


def test_field_cell_text_absent() -> None:
    entry = make_board_entry()
    assert _field_cell_text(entry, "ci") == ""


def test_field_cell_markup_with_color() -> None:
    entry = make_board_entry(cells={"ci": CellDisplay(text="failure", color="light red")})
    markup = _field_cell_markup(entry, "ci")
    assert isinstance(markup, tuple)
    assert markup[1] == "failure"


def test_field_cell_markup_no_color() -> None:
    entry = make_board_entry(cells={"pr": CellDisplay(text="#42")})
    markup = _field_cell_markup(entry, "pr")
    assert markup == "#42"


def test_field_cell_markup_absent() -> None:
    entry = make_board_entry()
    assert _field_cell_markup(entry, "pr") == ""


# =============================================================================
# Data source column defs
# =============================================================================


class _MockDataSource:
    @property
    def name(self) -> str:
        return "mock"

    @property
    def is_remote(self) -> bool:
        return False

    @property
    def columns(self) -> dict[str, str]:
        return {"mock_field": "MOCK", "another_field": "ANOTHER"}

    @property
    def field_types(self) -> dict[str, TypeAdapter[FieldValue]]:
        return {}

    def compute(
        self,
        agents: tuple[AgentDetails, ...],
        cached_fields: dict[AgentName, dict[str, FieldValue]],
        mngr_ctx: MngrContext,
    ) -> tuple[dict[AgentName, dict[str, FieldValue]], list[str]]:
        return {}, []


def test_build_data_source_column_defs() -> None:
    defs = _build_data_source_column_defs([_MockDataSource()])
    names = [d.name for d in defs]
    assert "mock_field" in names
    assert "another_field" in names


def test_build_data_source_column_defs_deduplicates() -> None:
    defs = _build_data_source_column_defs([_MockDataSource(), _MockDataSource()])
    names = [d.name for d in defs]
    assert names.count("mock_field") == 1


def test_resolve_board_layout_default_order() -> None:
    columns, section_order = resolve_board_layout([_MockDataSource()], KanpanPluginConfig())
    keys = [key for key, _header in columns]
    # Builtins come first, then the data source's columns appended (default order).
    assert keys[:2] == ["name", "state"]
    assert "mock_field" in keys
    # Headers are stripped of the TUI's display padding.
    assert ("name", "NAME") in columns
    assert ("mock_field", "MOCK") in columns
    assert section_order == BOARD_SECTION_ORDER


def test_resolve_board_layout_respects_configured_order() -> None:
    config = KanpanPluginConfig(
        column_order=["state", "name", "mock_field"],
        section_order=[BoardSection.MUTED, BoardSection.PR_MERGED],
    )
    columns, section_order = resolve_board_layout([_MockDataSource()], config)
    assert [key for key, _header in columns] == ["state", "name", "mock_field"]
    assert section_order == (BoardSection.MUTED, BoardSection.PR_MERGED)


# =============================================================================
# Field color palette
# =============================================================================


def test_build_field_color_palette_none_snapshot() -> None:
    entries, names = _build_field_color_palette(None)
    assert entries == []
    assert names == ()


def test_build_field_color_palette_with_colors() -> None:
    entry = make_board_entry(cells={"ci": CellDisplay(text="failure", color="light red")})
    snapshot = make_board_snapshot(entries=(entry,))
    entries, names = _build_field_color_palette(snapshot)
    assert len(entries) == 2
    assert "field_ci_light_red" in names


def test_build_field_color_palette_no_colors() -> None:
    entry = make_board_entry(cells={"pr": CellDisplay(text="#42")})
    snapshot = make_board_snapshot(entries=(entry,))
    entries, names = _build_field_color_palette(snapshot)
    assert entries == []


# =============================================================================
# Flatten markup
# =============================================================================


def test_flatten_markup_to_attr_muted_string() -> None:
    result = _flatten_markup_to_attr("hello", "muted")
    assert result == ("muted", "hello")


def test_flatten_markup_to_attr_muted_tuple() -> None:
    result = _flatten_markup_to_attr(("some_attr", "text"), "muted")
    assert result == ("muted", "text")


def test_flatten_markup_to_attr_muted_list() -> None:
    result = _flatten_markup_to_attr([("attr", "a"), "b"], "muted")
    assert result == ("muted", "ab")


# =============================================================================
# Staleness flatten + freshness predicate
# =============================================================================


def test_flatten_markup_to_attr_stale_string() -> None:
    assert _flatten_markup_to_attr("hello", "stale") == ("stale", "hello")


def test_flatten_markup_to_attr_stale_tuple() -> None:
    assert _flatten_markup_to_attr(("some_attr", "text"), "stale") == ("stale", "text")


def test_flatten_markup_to_attr_stale_list() -> None:
    assert _flatten_markup_to_attr([("attr", "a"), "b"], "stale") == ("stale", "ab")


def test_is_field_stale_old_field() -> None:
    now = datetime(2027, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    field = CommitsAheadField(count=3, has_work_dir=True, created=now - timedelta(seconds=3600))
    assert _is_field_stale(field, now, staleness_threshold_seconds=1800.0) is True


def test_is_field_stale_fresh_field() -> None:
    now = datetime(2027, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
    field = CommitsAheadField(count=3, has_work_dir=True, created=now - timedelta(seconds=60))
    assert _is_field_stale(field, now, staleness_threshold_seconds=1800.0) is False


def test_is_field_stale_at_threshold_boundary_is_not_stale() -> None:
    """Exactly at the threshold is not yet stale (strict >)."""
    now = datetime(2027, 1, 1, 0, 0, 3, tzinfo=timezone.utc)
    field = CommitsAheadField(count=3, has_work_dir=True, created=now - timedelta(seconds=1800))
    assert _is_field_stale(field, now, staleness_threshold_seconds=1800.0) is False


# =============================================================================
# _build_agent_row staleness rendering
# =============================================================================


def _make_ci_def() -> _ColumnDef:
    """Build a column def for the CI field, mirroring runtime construction."""
    return _ColumnDef(
        name=FIELD_CI,
        header="CI",
        text_fn=_FieldCellTextFn(field_key=FIELD_CI),
        markup_fn=_FieldCellMarkupFn(field_key=FIELD_CI),
        flexible=False,
    )


def _ci_widget_attr(row: Any) -> str | None:
    """Return the attribute name of the 'failure' CI cell in a built row, or None."""
    for widget, _options in row.contents:
        if not isinstance(widget, Text):
            continue
        text, attribs = widget.get_text()
        if text == "failure":
            return attribs[0][0] if attribs else None
    return None


def test_build_agent_row_stale_field_uses_stale_attr() -> None:
    now = datetime(2027, 1, 1, 0, 0, 4, tzinfo=timezone.utc)
    ci = CiField(status=CiStatus.FAILURE, created=now - timedelta(seconds=3600))
    entry = make_board_entry(
        section=BoardSection.STILL_COOKING,
        fields={FIELD_CI: ci},
        cells={FIELD_CI: ci.display()},
    )
    column_defs = [*_BUILTIN_COLUMN_DEFS, _make_ci_def()]
    widths = _compute_board_column_widths((entry,), column_defs)
    row = _build_agent_row(entry, widths, column_defs, now=now, staleness_threshold_seconds=1800.0)
    assert _ci_widget_attr(row) == "stale"


def test_build_agent_row_fresh_field_keeps_color_attr() -> None:
    now = datetime(2027, 1, 1, 0, 0, 5, tzinfo=timezone.utc)
    ci = CiField(status=CiStatus.FAILURE, created=now - timedelta(seconds=60))
    entry = make_board_entry(
        section=BoardSection.STILL_COOKING,
        fields={FIELD_CI: ci},
        cells={FIELD_CI: ci.display()},
    )
    column_defs = [*_BUILTIN_COLUMN_DEFS, _make_ci_def()]
    widths = _compute_board_column_widths((entry,), column_defs)
    row = _build_agent_row(entry, widths, column_defs, now=now, staleness_threshold_seconds=1800.0)
    assert _ci_widget_attr(row) == "field_ci_light_red"


def test_build_agent_row_muted_section_overrides_stale() -> None:
    """A muted row stays uniformly muted even if its fields are stale."""
    now = datetime(2027, 1, 1, 0, 0, 6, tzinfo=timezone.utc)
    ci = CiField(status=CiStatus.FAILURE, created=now - timedelta(seconds=3600))
    entry = make_board_entry(
        section=BoardSection.MUTED,
        fields={FIELD_CI: ci},
        cells={FIELD_CI: ci.display()},
    )
    column_defs = [*_BUILTIN_COLUMN_DEFS, _make_ci_def()]
    widths = _compute_board_column_widths((entry,), column_defs)
    row = _build_agent_row(entry, widths, column_defs, now=now, staleness_threshold_seconds=1800.0)
    assert _ci_widget_attr(row) == "muted"


# =============================================================================
# Carry forward fields
# =============================================================================


def test_carry_forward_fields_merges() -> None:
    old_entry = make_board_entry(
        name="a",
        fields={
            "pr": make_pr_field(created=datetime(2027, 1, 1, 0, 0, 7, tzinfo=timezone.utc)),
            "commits_ahead": CommitsAheadField(
                count=3, has_work_dir=True, created=datetime(2027, 1, 1, 0, 0, 8, tzinfo=timezone.utc)
            ),
        },
        cells={
            "pr": make_pr_field(created=datetime(2027, 1, 1, 0, 0, 9, tzinfo=timezone.utc)).display(),
            "commits_ahead": CommitsAheadField(
                count=3, has_work_dir=True, created=datetime(2027, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
            ).display(),
        },
    )
    new_entry = make_board_entry(
        name="a",
        fields={
            "commits_ahead": CommitsAheadField(
                count=5, has_work_dir=True, created=datetime(2027, 1, 1, 0, 0, 11, tzinfo=timezone.utc)
            )
        },
        cells={
            "commits_ahead": CommitsAheadField(
                count=5, has_work_dir=True, created=datetime(2027, 1, 1, 0, 0, 12, tzinfo=timezone.utc)
            ).display()
        },
    )
    old_snapshot = make_board_snapshot(entries=(old_entry,))
    new_snapshot = make_board_snapshot(entries=(new_entry,))
    result = _carry_forward_fields(old_snapshot, new_snapshot)
    merged = result.entries[0]
    assert "pr" in merged.fields
    assert "commits_ahead" in merged.fields
    ca_field = merged.fields["commits_ahead"]
    assert isinstance(ca_field, CommitsAheadField)
    assert ca_field.count == 5


def test_carry_forward_fields_blanks_a_cleared_label_cell() -> None:
    # What a user sees after clearing a label: the agent-only refresh that `refresh_afterwards`
    # triggers blanks the cell. Both halves are driven -- the label source produces the fields,
    # the merge lays them over the previous snapshot -- since either alone would still show
    # the stale value.
    source = LabelsDataSource(field_key="tag", config=LabelColumnConfig(header="TAG", label_key="tag"))
    mngr_ctx = make_mngr_ctx()
    name = AgentName("a")
    tagged, _ = source.compute(
        agents=(make_agent_details(name=str(name), labels={"tag": "blocked"}),),
        cached_fields={},
        mngr_ctx=mngr_ctx,
    )
    cleared, _ = source.compute(
        agents=(make_agent_details(name=str(name), labels={}),),
        cached_fields={},
        mngr_ctx=mngr_ctx,
    )
    old_snapshot = make_board_snapshot(entries=(_make_entry_from_fields(str(name), tagged[name]),))
    new_snapshot = make_board_snapshot(entries=(_make_entry_from_fields(str(name), cleared[name]),))
    assert old_snapshot.entries[0].cells["tag"].text == "blocked"
    merged = _carry_forward_fields(old_snapshot, new_snapshot).entries[0]
    assert merged.cells["tag"].text == ""


def test_carry_forward_fields_new_agent() -> None:
    new_entry = make_board_entry(name="new-agent")
    old_snapshot = make_board_snapshot(entries=())
    new_snapshot = make_board_snapshot(entries=(new_entry,))
    result = _carry_forward_fields(old_snapshot, new_snapshot)
    assert len(result.entries) == 1
    assert result.entries[0].name == AgentName("new-agent")


# =============================================================================
# _FieldCellTextFn, _FieldCellMarkupFn
# =============================================================================


def test_field_cell_text_fn_call() -> None:
    entry = make_board_entry(cells={"pr": CellDisplay(text="#1")})
    fn = _FieldCellTextFn(field_key="pr")
    assert fn(entry) == "#1"


def test_field_cell_markup_fn_call() -> None:
    entry = make_board_entry(cells={"pr": CellDisplay(text="#1")})
    fn = _FieldCellMarkupFn(field_key="pr")
    assert fn(entry) == "#1"


# =============================================================================
# CI field markup - color is always provided by CiField.display()
# =============================================================================


def test_field_cell_markup_ci_failure_uses_color_attr() -> None:
    """CI FAILURE cell has color='light red', so markup uses field_ci_light_red attr."""
    ci = CiField(status=CiStatus.FAILURE, created=datetime(2027, 1, 1, 0, 0, 13, tzinfo=timezone.utc))
    cell = ci.display()
    entry = make_board_entry(
        fields={FIELD_CI: ci},
        cells={FIELD_CI: cell},
    )
    markup = _field_cell_markup(entry, FIELD_CI)
    assert isinstance(markup, tuple)
    assert markup[0] == f"field_{FIELD_CI}_light_red"
    assert markup[1] == cell.text


def test_field_cell_markup_ci_pending_uses_color_attr() -> None:
    """CI PENDING cell has color='yellow', so markup uses field_ci_yellow attr."""
    ci = CiField(status=CiStatus.PENDING, created=datetime(2027, 1, 1, 0, 0, 14, tzinfo=timezone.utc))
    cell = ci.display()
    entry = make_board_entry(
        fields={FIELD_CI: ci},
        cells={FIELD_CI: cell},
    )
    markup = _field_cell_markup(entry, FIELD_CI)
    assert isinstance(markup, tuple)
    assert markup[0] == f"field_{FIELD_CI}_yellow"
    assert markup[1] == cell.text


def test_field_cell_markup_ci_success_uses_color_attr() -> None:
    """CI SUCCESS cell has color='light green', so markup uses field_ci_light_green attr."""
    ci = CiField(status=CiStatus.SUCCESS, created=datetime(2027, 1, 1, 0, 0, 15, tzinfo=timezone.utc))
    cell = ci.display()
    entry = make_board_entry(
        fields={FIELD_CI: ci},
        cells={FIELD_CI: cell},
    )
    markup = _field_cell_markup(entry, FIELD_CI)
    assert isinstance(markup, tuple)
    assert markup[0] == f"field_{FIELD_CI}_light_green"
    assert markup[1] == cell.text


# =============================================================================
# _compute_board_column_widths
# =============================================================================


def test_compute_board_column_widths_empty_entries() -> None:
    widths = _compute_board_column_widths((), _BUILTIN_COLUMN_DEFS)
    # name col header is "  NAME" (6), state col header is "STATE" (5)
    assert widths["name"] == len("  NAME")
    assert widths["state"] == len("STATE")


def test_compute_board_column_widths_with_entries() -> None:
    entry = make_board_entry(name="a-long-agent-name-here")
    widths = _compute_board_column_widths((entry,), _BUILTIN_COLUMN_DEFS)
    # "  a-long-agent-name-here" is longer than "  NAME"
    assert widths["name"] > len("  NAME")


# =============================================================================
# _build_board_widgets with marks and muted entries
# =============================================================================


def test_build_board_widgets_with_marks() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    snapshot = make_board_snapshot(entries=(entry,))
    marks = {AgentName("agent-a"): "d"}
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS, marks=marks)
    assert len(idx_map) == 1


def test_build_board_widgets_muted_entry() -> None:
    entry = make_board_entry(name="muted-agent", is_muted=True, section=BoardSection.MUTED)
    snapshot = make_board_snapshot(entries=(entry,))
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert len(idx_map) == 1


def test_build_board_widgets_multiple_sections() -> None:
    e1 = make_board_entry(name="a", section=BoardSection.STILL_COOKING)
    e2 = make_board_entry(name="b", section=BoardSection.PR_BEING_REVIEWED)
    e3 = make_board_entry(name="c", section=BoardSection.PRS_FAILED)
    snapshot = make_board_snapshot(entries=(e1, e2, e3))
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert len(idx_map) == 3


# =============================================================================
# _update_row_mark
# =============================================================================


def test_update_row_mark_no_walker() -> None:
    state = _make_state()
    # Should not raise even with no walker
    _update_row_mark(state, 0, "d")


def test_update_row_mark_no_entry_at_index() -> None:
    state = _make_state()
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    snapshot = make_board_snapshot(entries=(entry,))
    state.snapshot = snapshot
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    state.list_walker = walker
    state.index_to_entry = idx_map
    # Index 0 is the header row, not an agent entry; should not raise
    _update_row_mark(state, 0, "d")


# =============================================================================
# _toggle_mark
# =============================================================================


def _make_state_with_walker(entries: tuple[AgentBoardEntry, ...]) -> _KanpanState:
    """Build a state with a populated list walker from entries."""
    commands = {
        "d": CustomCommand(name="delete", markable="light red"),
        "p": CustomCommand(name="push", markable="yellow"),
    }
    state = _make_state(snapshot=make_board_snapshot(entries=entries), commands=commands)
    state.mark_attr_names = ("mark_d", "mark_p")
    walker, idx_map = _build_board_widgets(make_board_snapshot(entries=entries), _BUILTIN_COLUMN_DEFS)
    state.list_walker = walker
    state.index_to_entry = idx_map
    return state


def test_toggle_mark_adds_mark() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    # Find the index of the agent entry
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    _toggle_mark(state, "d")
    assert AgentName("agent-a") in state.marks
    assert state.marks[AgentName("agent-a")] == "d"


def test_toggle_mark_removes_existing_mark() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    state.marks[AgentName("agent-a")] = "d"
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    _toggle_mark(state, "d")
    assert AgentName("agent-a") not in state.marks


def test_toggle_mark_no_walker() -> None:
    # No walker means no-op; should not raise
    state = _make_state()
    _toggle_mark(state, "d")


# =============================================================================
# _unmark_focused
# =============================================================================


def test_unmark_focused_removes_mark() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    state.marks[AgentName("agent-a")] = "d"
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    _unmark_focused(state)
    assert AgentName("agent-a") not in state.marks


def test_unmark_focused_no_mark_is_noop() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    _unmark_focused(state)


# =============================================================================
# _unmark_all
# =============================================================================


def test_unmark_all_clears_marks() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    state.marks[AgentName("agent-a")] = "d"
    _unmark_all(state)
    assert state.marks == {}


def test_unmark_all_empty_marks_noop() -> None:
    state = _make_state()
    _unmark_all(state)


# =============================================================================
# _update_mark_count_footer
# =============================================================================


def test_update_mark_count_footer_with_marks() -> None:
    commands = {"d": CustomCommand(name="delete", markable="light red")}
    state = _make_state(commands=commands)
    state.marks = {AgentName("agent-a"): "d", AgentName("agent-b"): "d"}
    _update_mark_count_footer(state)
    assert "delete" in state.footer_left_text.text or "d" in state.footer_left_text.text


def test_update_mark_count_footer_no_marks_restores_footer() -> None:
    state = _make_state()
    state.steady_footer_text = "  Steady"
    state.marks = {}
    _update_mark_count_footer(state)
    assert state.footer_left_text.text == "  Steady"


# =============================================================================
# _execute_marks
# =============================================================================


def test_execute_marks_no_marks_does_nothing() -> None:
    state = _make_state()
    state.marks = {}
    _execute_marks(state)


def test_execute_marks_already_executing_does_nothing() -> None:
    state = _make_state()
    state.marks = {AgentName("a"): "d"}
    state.executing = True
    _execute_marks(state)


# =============================================================================
# _prune_orphaned_marks (full coverage including orphaned branch)
# =============================================================================


def test_prune_orphaned_marks_with_orphans() -> None:
    commands = {"d": CustomCommand(name="delete", markable="light red")}
    state = _make_state(commands=commands)
    state.steady_footer_text = "  Steady"
    state.marks = {AgentName("gone-agent"): "d"}
    state.snapshot = make_board_snapshot(entries=())
    _prune_orphaned_marks(state)
    assert AgentName("gone-agent") not in state.marks


# =============================================================================
# _dispatch_command
# =============================================================================


def test_dispatch_command_markable_key_toggles_mark() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    commands: dict[str, KanpanCommand] = {"d": CustomCommand(name="delete", markable="light red")}
    state = _make_state_with_walker((entry,))
    state.commands = commands
    state.mark_attr_names = ("mark_d",)
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    _dispatch_command(state, "d", commands["d"])
    assert AgentName("agent-a") in state.marks


def test_dispatch_command_unmark_key_removes_mark() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    state.marks[AgentName("agent-a")] = "d"
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    unmark_cmd = ActionBuiltinCommand(role=ActionBuiltinRole.UNMARK, name="unmark")
    state.commands = {_BUILTIN_COMMAND_KEY_UNMARK: unmark_cmd}
    _dispatch_command(state, _BUILTIN_COMMAND_KEY_UNMARK, unmark_cmd)
    assert AgentName("agent-a") not in state.marks


def test_dispatch_command_execute_key_with_marks(tmp_path: Path) -> None:
    # Use a non-builtin key ("z") so the test isn't entangled with builtin
    # dispatch semantics. The command touches a marker file, and we assert it
    # appears after executor shutdown -- proving the command actually ran
    # (rather than just that state.executing was set).
    marker = tmp_path / "executed"
    assert not marker.exists()
    mark_cmd = CustomCommand(name="do-thing", command=f"touch {marker}")
    state = _make_state(commands={"z": mark_cmd})
    state.marks = {AgentName("a"): "z"}
    execute_cmd = ActionBuiltinCommand(role=ActionBuiltinRole.EXECUTE, name="execute")
    _dispatch_command(state, _BUILTIN_COMMAND_KEY_EXECUTE, execute_cmd)
    # Should start batch execution (sets executing=True; with loop=None the
    # future is submitted but never polled, so executing stays True).
    assert state.executing is True
    assert state.batch_executor is not None
    state.batch_executor.shutdown(wait=True)
    assert marker.exists()


def test_dispatch_command_execute_user_override_of_delete_runs_shell(tmp_path: Path) -> None:
    # Overriding the builtin "d" (delete) must route to the user's shell
    # command, not to the hardcoded `mngr destroy` runner.
    marker = tmp_path / "ran"
    assert not marker.exists()
    override = CustomCommand(name="my-delete", command=f"touch {marker}", markable="light red")
    state = _make_state(commands={_BUILTIN_COMMAND_KEY_DELETE: override})
    state.marks = {AgentName("a"): _BUILTIN_COMMAND_KEY_DELETE}
    execute_cmd = ActionBuiltinCommand(role=ActionBuiltinRole.EXECUTE, name="execute")
    _dispatch_command(state, _BUILTIN_COMMAND_KEY_EXECUTE, execute_cmd)
    assert state.executing is True
    assert state.batch_executor is not None
    state.batch_executor.shutdown(wait=True)
    assert marker.exists()


# =============================================================================
# _refresh_display
# =============================================================================


def test_refresh_display_updates_walker() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    snapshot = make_board_snapshot(entries=(entry,))
    state = _make_state(snapshot=snapshot)
    _refresh_display(state)
    assert state.list_walker is not None
    assert len(state.index_to_entry) == 1


def test_refresh_display_restores_focus() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    snapshot = make_board_snapshot(entries=(entry,))
    state = _make_state(snapshot=snapshot)
    state.focused_agent_name = AgentName("agent-a")
    _refresh_display(state)
    # Focus should be on the entry if it's still present
    assert state.list_walker is not None


def test_refresh_display_none_snapshot() -> None:
    state = _make_state()
    state.snapshot = None
    _refresh_display(state)
    assert state.list_walker is not None


def test_refresh_display_writes_the_header_status() -> None:
    """Every path that changes the board goes through _refresh_display, which owns the header."""
    snapshot = make_board_snapshot(
        entries=(
            make_board_entry(name="a", state=AgentLifecycleState.RUNNING),
            make_board_entry(name="b", state=AgentLifecycleState.STOPPED),
        )
    )
    state = _make_state(snapshot=snapshot)
    state.header_status = compile_header_status('{state == "RUNNING"} running / {total}')
    _refresh_display(state)
    assert state.header_status_text.text.strip() == "1 running / 2"


# =============================================================================
# _load_user_commands and _build_command_map
# =============================================================================


def test_load_user_commands_from_custom_command_instance() -> None:
    cmd = CustomCommand(name="my-cmd", command="echo hi")
    config = KanpanPluginConfig(commands={"c": cmd})
    ctx = make_mngr_ctx_with_config(config)
    result = _load_user_commands(ctx)
    assert "c" in result
    assert result["c"].name == "my-cmd"


def test_load_user_commands_from_dict() -> None:
    config = KanpanPluginConfig(commands={"c": CustomCommand(name="my-cmd", command="echo hi")})
    ctx = make_mngr_ctx_with_config(config)
    result = _load_user_commands(ctx)
    assert "c" in result


def test_load_user_commands_from_raw_dict_via_model_construct() -> None:
    # Regression: the mngr config loader uses `model_construct` which bypasses
    # Pydantic's recursive validation, leaving `commands` entries as raw dicts
    # rather than `CustomCommand` instances. `_load_user_commands` must handle
    # both shapes.
    config = KanpanPluginConfig.model_construct(
        commands={"c": {"name": "dict-cmd", "command": "echo hi"}},
    )
    ctx = make_mngr_ctx_with_config(config)
    result = _load_user_commands(ctx)
    assert "c" in result
    assert isinstance(result["c"], CustomCommand)
    assert result["c"].name == "dict-cmd"


def test_load_user_commands_rejects_builtin_kind_in_raw_dict() -> None:
    # A user cannot hijack the builtin-dispatch path (e.g. `mngr destroy`) by
    # setting `kind = "builtin"` in their TOML config. `CustomCommand.kind` is
    # `Literal["user"]`, so Pydantic validation rejects the raw dict when
    # `_load_user_commands` constructs a `CustomCommand` from it.
    config = KanpanPluginConfig.model_construct(
        commands={"c": {"kind": "builtin", "name": "sneaky"}},
    )
    ctx = make_mngr_ctx_with_config(config)
    with pytest.raises(ValidationError):
        _load_user_commands(ctx)


def test_build_command_map_includes_builtins() -> None:
    config = KanpanPluginConfig()
    ctx = make_mngr_ctx_with_config(config)
    result = _build_command_map(ctx)
    # "r" is the builtin refresh key; "q" is quit and not a mapped command
    assert "r" in result
    assert "q" not in result


def test_build_command_map_user_overrides_builtin() -> None:
    custom = CustomCommand(name="my-refresh", command="echo refresh")
    config = KanpanPluginConfig(commands={_BUILTIN_COMMAND_KEY_REFRESH: custom})
    ctx = make_mngr_ctx_with_config(config)
    result = _build_command_map(ctx)
    assert result[_BUILTIN_COMMAND_KEY_REFRESH].name == "my-refresh"


def test_build_command_map_excludes_disabled() -> None:
    disabled = CustomCommand(name="disabled-cmd", enabled=False)
    config = KanpanPluginConfig(commands={"z": disabled})
    ctx = make_mngr_ctx_with_config(config)
    result = _build_command_map(ctx)
    assert "z" not in result


# =============================================================================
# _update_snapshot_mute: None snapshot branch
# =============================================================================


def test_update_snapshot_mute_none_snapshot() -> None:
    # When snapshot is None, function should return without error
    state = _make_state()
    state.snapshot = None
    _update_snapshot_mute(state, AgentName("agent"), True)


# =============================================================================
# _assemble_column_defs: empty result fallback
# =============================================================================


def test_assemble_column_defs_empty_order_falls_back_to_builtins() -> None:
    result = _assemble_column_defs(_BUILTIN_COLUMN_DEFS, [], ["nonexistent"])
    # All names unknown => result is empty => falls back to builtins
    assert len(result) == len(_BUILTIN_COLUMN_DEFS)


# =============================================================================
# _KanpanInputHandler: "U" key, command dispatch, up/down keys
# =============================================================================


def test_input_handler_U_key_clears_marks() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    state.marks = {AgentName("agent-a"): "d"}
    handler = _KanpanInputHandler(state=state)
    result = handler("U")
    assert result is True
    assert state.marks == {}


def test_input_handler_command_key_dispatches() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    handler = _KanpanInputHandler(state=state)
    result = handler("d")
    assert result is True
    assert AgentName("agent-a") in state.marks


def test_input_handler_up_key_not_first_passes_through() -> None:
    entry1 = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    entry2 = make_board_entry(name="agent-b", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry1, entry2))
    b_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-b"))
    state.list_walker.set_focus(b_idx)
    handler = _KanpanInputHandler(state=state)
    result = handler("up")
    assert result is None


def test_input_handler_up_key_on_first_clears_focus() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    a_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(a_idx)
    handler = _KanpanInputHandler(state=state)
    result = handler("up")
    assert result is True
    assert state.focused_agent_name is None


def test_input_handler_down_key_passes_through() -> None:
    state = _make_state()
    handler = _KanpanInputHandler(state=state)
    assert handler("down") is None


def test_input_handler_page_up_passes_through() -> None:
    state = _make_state()
    handler = _KanpanInputHandler(state=state)
    assert handler("page up") is None


# =============================================================================
# _is_focus_on_first_selectable
# =============================================================================


def test_is_focus_on_first_selectable_no_walker() -> None:
    state = _make_state()
    assert _is_focus_on_first_selectable(state) is False


def test_is_focus_on_first_selectable_at_first() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    a_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(a_idx)
    assert _is_focus_on_first_selectable(state) is True


def test_is_focus_on_first_selectable_at_non_first() -> None:
    entry1 = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    entry2 = make_board_entry(name="agent-b", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry1, entry2))
    b_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-b"))
    state.list_walker.set_focus(b_idx)
    assert _is_focus_on_first_selectable(state) is False


# =============================================================================
# _get_focused_entry
# =============================================================================


def test_get_focused_entry_no_walker() -> None:
    state = _make_state()
    assert _get_focused_entry(state) is None


def test_get_focused_entry_with_focus() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    a_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(a_idx)
    result = _get_focused_entry(state)
    assert result is not None
    assert result.name == AgentName("agent-a")


def test_get_focused_entry_no_focus() -> None:
    state = _make_state()
    assert _get_focused_entry(state) is None


# =============================================================================
# _update_row_mark: muted entry path
# =============================================================================


def test_update_row_mark_muted_entry() -> None:
    entry = make_board_entry(name="muted-agent", is_muted=True, section=BoardSection.MUTED)
    snapshot = make_board_snapshot(entries=(entry,))
    state = _make_state(snapshot=snapshot)
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    state.list_walker = walker
    state.index_to_entry = idx_map
    agent_idx = next(k for k, v in idx_map.items() if v.name == AgentName("muted-agent"))
    _update_row_mark(state, agent_idx, "d")


# =============================================================================
# _toggle_mark: push with no work_dir
# =============================================================================


def test_toggle_mark_push_no_work_dir_shows_message() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    commands = {
        _BUILTIN_COMMAND_KEY_PUSH: CustomCommand(name="mark push", markable="yellow"),
    }
    state = _make_state(snapshot=make_board_snapshot(entries=(entry,)), commands=commands)
    state.mark_attr_names = ("mark_p",)
    walker, idx_map = _build_board_widgets(make_board_snapshot(entries=(entry,)), _BUILTIN_COLUMN_DEFS)
    state.list_walker = walker
    state.index_to_entry = idx_map
    a_idx = next(k for k, v in idx_map.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(a_idx)
    _toggle_mark(state, _BUILTIN_COMMAND_KEY_PUSH)
    assert AgentName("agent-a") not in state.marks
    assert "Cannot push" in state.footer_left_text.text


# =============================================================================
# _finish_batch_execution
# =============================================================================


def test_finish_batch_execution_all_ok() -> None:
    state = _make_state()
    state.executing = True
    _finish_batch_execution(
        state,
        [_BatchItemResult(label="op1", is_success=True), _BatchItemResult(label="op2", is_success=True)],
    )
    assert state.executing is False
    assert "2" in state.footer_left_text.text
    assert state.execute_errors == ()


def test_finish_batch_execution_with_failures() -> None:
    state = _make_state()
    state.executing = True
    _finish_batch_execution(
        state,
        [
            _BatchItemResult(label="op1", is_success=True),
            _BatchItemResult(label="op2", is_success=False, detail="boom"),
        ],
    )
    assert state.executing is False
    assert "1 failed" in state.footer_left_text.text
    # The failure detail is persisted for rendering at the bottom of the board.
    assert state.execute_errors == ("op2: boom",)


def test_finish_batch_execution_empty_results() -> None:
    state = _make_state()
    state.executing = True
    _finish_batch_execution(state, [])
    assert state.executing is False
    assert state.execute_errors == ()


# =============================================================================
# Batch outcomes and the poll that collects them
# =============================================================================


def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def _make_done_future(result: subprocess.CompletedProcess[str]) -> "Future[subprocess.CompletedProcess[str]]":
    """Create an already-completed future with a given result."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut: Future[subprocess.CompletedProcess[str]] = pool.submit(lambda: result)
        fut.result()
    return fut


def _batch_item(name: str = "agent-a", key: str = "c", **kwargs: Any) -> _BatchWorkItem:
    return _BatchWorkItem(name=AgentName(name), key=key, cmd=CustomCommand(name="custom"), entry=None, **kwargs)


def test_record_batch_result_clears_the_mark_on_success() -> None:
    state = _make_state()
    item = _batch_item()
    state.marks = {AgentName("agent-a"): "c"}
    results: list[_BatchItemResult] = []
    _record_batch_result(state, item, _make_done_future(_completed(0)), results)
    assert [r.is_success for r in results] == [True]
    assert AgentName("agent-a") not in state.marks


def test_record_batch_result_keeps_the_mark_and_the_stderr_on_failure() -> None:
    state = _make_state()
    item = _batch_item()
    state.marks = {AgentName("agent-a"): "c"}
    results: list[_BatchItemResult] = []
    _record_batch_result(state, item, _make_done_future(_completed(1, "something bad")), results)
    assert results[0].is_success is False
    assert results[0].detail == "something bad"
    # The surviving mark is the retry set.
    assert AgentName("agent-a") in state.marks


def test_record_batch_result_reports_a_timeout_in_words() -> None:
    state = _make_state()

    def _raise_timeout() -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["mngr", "destroy"], timeout=60)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future: Future[subprocess.CompletedProcess[str]] = pool.submit(_raise_timeout)
        future.exception()
        results: list[_BatchItemResult] = []
        _record_batch_result(state, _batch_item(), future, results)
    assert results[0].detail == "timed out after 60s"


def test_record_batch_result_clears_every_mark_the_delete_call_covered() -> None:
    # The builtin delete puts all marked agents through one `mngr destroy`, so its single
    # result stands for each of them.
    state = _make_state()
    item = _batch_item(name="a", key=_BUILTIN_COMMAND_KEY_DELETE, batch_names=(AgentName("a"), AgentName("b")))
    state.marks = {AgentName("a"): _BUILTIN_COMMAND_KEY_DELETE, AgentName("b"): _BUILTIN_COMMAND_KEY_DELETE}
    results: list[_BatchItemResult] = []
    _record_batch_result(state, item, _make_done_future(_completed(0)), results)
    assert state.marks == {}
    assert len(results) == 2


def test_on_batch_poll_keeps_watching_while_anything_is_still_running() -> None:
    state = _make_state()
    state.executing = True
    with ThreadPoolExecutor(max_workers=1) as pool:
        barrier = threading.Barrier(2)

        def _wait() -> subprocess.CompletedProcess[str]:
            barrier.wait()
            return _completed(0)

        future: Future[subprocess.CompletedProcess[str]] = pool.submit(_wait)
        mock_loop = _make_mock_loop()
        _on_batch_poll(mock_loop, (state, [(_batch_item(), future)], [], 1))
        # Still in flight, so the batch is not finished and the watch is rescheduled.
        assert state.executing is True
        assert mock_loop._alarm_tracker.call_count >= 1
        barrier.wait()


def test_on_batch_poll_finishes_once_the_last_one_lands() -> None:
    state = _make_state()
    state.executing = True
    state.loop = _make_mock_loop()
    results: list[_BatchItemResult] = []
    _on_batch_poll(state.loop, (state, [(_batch_item(), _make_done_future(_completed(0)))], results, 1))
    assert state.executing is False


# =============================================================================
# _submit_batch_item
# =============================================================================


def test_submit_batch_item_push_with_work_dir(tmp_path: Path) -> None:
    entry = AgentBoardEntry(
        name=AgentName("agent-a"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
        work_dir=tmp_path,
    )
    item = _BatchWorkItem(
        name=AgentName("agent-a"),
        key=_BUILTIN_COMMAND_KEY_PUSH,
        cmd=MarkableBuiltinCommand(role=MarkableBuiltinRole.PUSH, name="push", markable="yellow"),
        entry=entry,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = _submit_batch_item(pool, item)
        assert future is not None
        future.cancel()


def test_submit_batch_item_push_no_work_dir() -> None:
    entry = make_board_entry(name="agent-a")
    item = _BatchWorkItem(
        name=AgentName("agent-a"),
        key=_BUILTIN_COMMAND_KEY_PUSH,
        cmd=MarkableBuiltinCommand(role=MarkableBuiltinRole.PUSH, name="push", markable="yellow"),
        entry=entry,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = _submit_batch_item(pool, item)
    assert future is None


def test_submit_batch_item_shell_command() -> None:
    item = _BatchWorkItem(
        name=AgentName("agent-a"),
        key="c",
        cmd=CustomCommand(name="custom", command="true"),
        entry=None,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = _submit_batch_item(pool, item)
        assert future is not None
        future.result(timeout=5)


def test_submit_batch_item_no_command_returns_none() -> None:
    item = _BatchWorkItem(
        name=AgentName("agent-a"),
        key="c",
        cmd=CustomCommand(name="custom"),
        entry=None,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = _submit_batch_item(pool, item)
    assert future is None


# =============================================================================
# _run_shell_command (loop=None, no alarm)
# =============================================================================


def test_run_shell_command_submits_future() -> None:
    entry = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    a_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(a_idx)
    cmd = CustomCommand(name="say-hi", command="true")
    _run_shell_command(state, cmd)
    assert state.executor is not None
    state.executor.shutdown(wait=True)


# =============================================================================
# _execute_batch: skipped item (future is None)
# =============================================================================


def test_execute_batch_reports_an_item_it_cannot_run_as_skipped() -> None:
    # A command with nothing to run yields no future; it must still be accounted for,
    # or the batch would quietly report fewer operations than were marked.
    state = _make_state()
    item = _BatchWorkItem(name=AgentName("agent-a"), key="c", cmd=CustomCommand(name="noop"), entry=None)
    _execute_batch(state, [item])
    assert state.executing is False
    assert any("skipped" in error for error in state.execute_errors)


# =============================================================================
# Tests for _build_board_widgets section_order parameter
# =============================================================================


def _extract_section_headings(walker: Any) -> list[str]:
    """Extract plain-text section heading strings from a walker."""
    headings: list[str] = []
    for widget in walker:
        if isinstance(widget, Text):
            text = widget.get_text()[0]
            if " (" in text and (
                "Done" in text
                or "In progress" in text
                or "In review" in text
                or "Muted" in text
                or "Cancelled" in text
            ):
                headings.append(text)
    return headings


def test_build_board_widgets_default_section_order() -> None:
    entries = (
        make_board_entry(name="cooking"),
        make_board_entry(name="merged", section=BoardSection.PR_MERGED),
    )
    walker, _ = _build_board_widgets(make_board_snapshot(entries=entries), _BUILTIN_COLUMN_DEFS)
    headings = _extract_section_headings(walker)
    assert len(headings) == 2
    assert "Done" in headings[0]
    assert "In progress" in headings[1]


def test_build_board_widgets_custom_section_order_reverses() -> None:
    entries = (
        make_board_entry(name="cooking"),
        make_board_entry(name="merged", section=BoardSection.PR_MERGED),
    )
    reversed_order = (BoardSection.STILL_COOKING, BoardSection.PR_MERGED)
    walker, _ = _build_board_widgets(
        make_board_snapshot(entries=entries),
        _BUILTIN_COLUMN_DEFS,
        section_order=reversed_order,
    )
    headings = _extract_section_headings(walker)
    assert len(headings) == 2
    assert "In progress" in headings[0]
    assert "Done" in headings[1]


def test_build_board_widgets_section_order_omits_unlisted() -> None:
    entries = (
        make_board_entry(name="cooking"),
        make_board_entry(name="merged", section=BoardSection.PR_MERGED),
    )
    only_merged = (BoardSection.PR_MERGED,)
    walker, index_to_entry = _build_board_widgets(
        make_board_snapshot(entries=entries),
        _BUILTIN_COLUMN_DEFS,
        section_order=only_merged,
    )
    headings = _extract_section_headings(walker)
    assert len(headings) == 1
    assert "Done" in headings[0]
    assert len(index_to_entry) == 1


# =============================================================================
# Tests for _resolve_section_order
# =============================================================================


def test_resolve_section_order_none_returns_default() -> None:
    assert _resolve_section_order(None) == BOARD_SECTION_ORDER


def test_resolve_section_order_custom_list() -> None:
    custom = [BoardSection.STILL_COOKING, BoardSection.MUTED]
    result = _resolve_section_order(custom)
    assert result == (BoardSection.STILL_COOKING, BoardSection.MUTED)


# =============================================================================
# Peek panel
# =============================================================================


def test_last_nonempty_line_returns_last_meaningful_line() -> None:
    assert _last_nonempty_line("first\nsecond\n\n") == "second"


def test_last_nonempty_line_all_blank_returns_empty() -> None:
    assert _last_nonempty_line("   \n\n") == ""


def test_peek_body_lines_tails_to_window_with_marker() -> None:
    transcript = "\n".join(f"line{i}" for i in range(30)) + "\n\n\n"
    lines = _peek_body_lines(transcript, [])
    # Trailing blanks dropped; only the newest PEEK_BODY_HEIGHT lines show, under a ⋯ marker.
    assert lines[0] == "⋯"
    assert lines[-1] == "line29"
    assert len(lines) == PEEK_BODY_HEIGHT + 1


def test_peek_body_lines_short_message_has_no_marker() -> None:
    transcript = "[2026-07-07T00:14:35Z] assistant:\nall done, tests pass\n"
    lines = _peek_body_lines(transcript, [])
    assert "⋯" not in lines
    assert lines[-1] == "all done, tests pass"


def test_peek_body_lines_empty_transcript_is_empty() -> None:
    assert _peek_body_lines("\n\n", []) == []


def test_peek_body_lines_appends_pending_reply() -> None:
    lines = _peek_body_lines("[..] assistant:\nhi", ["my reply"])
    # A sent-but-not-yet-echoed reply is appended as a `›` line so it shows immediately.
    assert lines[-1] == "› my reply"


def test_peek_body_markup_empty_says_no_messages() -> None:
    assert _peek_body_markup("", []) == [("peek_hint", "(no messages yet)")]


def test_peek_body_markup_dims_headers_and_accents_replies() -> None:
    markup = _peek_body_markup("[2026-07-07T00:14:35Z] user:\nhello", ["a reply"])
    # Header shortened + dimmed, content plain, pending reply accented.
    assert ("peek_hint", "user:") in markup
    assert "hello" in markup
    assert ("peek_user", "› a reply") in markup


def test_short_header_drops_timestamp() -> None:
    assert _short_header("[2026-07-07T00:14:35Z] assistant:") == "assistant:"
    assert _short_header("no-bracket line") == "no-bracket line"


def test_is_transcript_header_matches_only_headers() -> None:
    assert _is_transcript_header("[2026-07-07T00:14:35Z] user:")
    assert not _is_transcript_header("just some text")
    assert not _is_transcript_header("  -> Bash(ls)")


def test_peek_reply_executor_serializes_in_order() -> None:
    state = _make_state()
    executor = _ensure_peek_reply_executor(state)
    # A single worker guarantees FIFO, so several queued replies reach the agent in
    # the order they were typed rather than their pastes interleaving.
    assert executor._max_workers == 1
    order: list[int] = []
    futures = [executor.submit(order.append, i) for i in range(6)]
    for future in futures:
        future.result()
    assert order == [0, 1, 2, 3, 4, 5]
    # The same executor is reused across submits (not recreated per reply).
    assert _ensure_peek_reply_executor(state) is executor
    executor.shutdown(wait=True)


def test_close_peek_restores_footer_and_clears_state() -> None:
    entry = make_board_entry(name="agent-a")
    state = _make_state_with_walker((entry,))
    original_footer = Text("keybinding-bar")
    state.frame.footer = original_footer
    # Simulate an open panel.
    state.peek_agent_name = AgentName("agent-a")
    state.saved_footer = original_footer
    state.frame.footer = Text("peek-panel")
    state.peek_body_text = Text("body")
    state.peek_input = Text("reply")

    _close_peek(state)

    assert state.peek_agent_name is None
    assert state.frame.footer is original_footer
    assert state.peek_body_text is None


def test_close_peek_when_not_open_is_noop() -> None:
    state = _make_state()
    _close_peek(state)
    assert state.peek_agent_name is None


def test_find_entry_by_name_found_and_missing() -> None:
    entry = make_board_entry(name="agent-a")
    state = _make_state_with_walker((entry,))
    assert _find_entry_by_name(state, AgentName("agent-a")) is not None
    assert _find_entry_by_name(state, AgentName("nope")) is None
    assert _find_entry_by_name(state, None) is None


def test_focus_row_by_name_moves_walker_focus() -> None:
    entries = (make_board_entry(name="agent-a"), make_board_entry(name="agent-b"))
    state = _make_state_with_walker(entries)
    _focus_row_by_name(state, AgentName("agent-b"))
    _, focus_idx = state.list_walker.get_focus()
    assert state.index_to_entry[focus_idx].name == AgentName("agent-b")


def test_ensure_peek_executor_is_created_once() -> None:
    state = _make_state()
    first = _ensure_peek_executor(state)
    second = _ensure_peek_executor(state)
    assert first is second
    first.shutdown(wait=False)


def test_build_peek_panel_populates_parts() -> None:
    state = _make_state()
    _build_peek_panel(state)
    assert state.peek_input is not None
    assert state.peek_input.get_edit_text() == ""
    assert state.peek_body_text is not None
    assert state.peek_box is not None


def test_legend_markup_styles_keys_and_keeps_units_unwrappable() -> None:
    markup = _legend_markup([("p", "push"), ("U", "unmark all")], "footer_key", "footer", "  ")
    assert markup == [
        ("footer_key", "p"),
        ("footer", ":\u00a0push"),
        ("footer", "  "),
        ("footer_key", "U"),
        ("footer", ":\u00a0unmark\u00a0all"),
    ]


def test_legend_markup_single_binding_has_no_separator() -> None:
    assert _legend_markup([("q", "quit")], "k", "t", " · ") == [("k", "q"), ("t", ":\u00a0quit")]


def test_write_terminal_title_emits_osc_zero() -> None:
    out = io.StringIO()
    _write_terminal_title(Screen(output=out), "kanpan")
    assert out.getvalue() == "\x1b]0;kanpan\x07"


def test_legend_bindings_overlay_includes_user_custom_commands() -> None:
    commands: dict[str, KanpanCommand] = {
        **_BUILTIN_COMMANDS,
        "z": CustomCommand(name="zap logs"),
        "b": CustomCommand(name="backup", markable="light red"),
    }
    overlay_bindings, footer_legend = _build_legend_bindings(commands)
    assert ("z", "zap logs") in overlay_bindings
    assert ("b", "backup") in overlay_bindings
    overlay_keys = [key for key, _ in overlay_bindings]
    assert overlay_keys[:2] == ["space", "enter"]
    assert overlay_keys[-2:] == ["q", "?"]
    footer_keys = [key for key, _ in footer_legend]
    assert footer_keys == ["/", "r", "m", "d", "x", "q", "?"]


def test_legend_bindings_footer_follows_command_overrides() -> None:
    commands: dict[str, KanpanCommand] = {**_BUILTIN_COMMANDS, "m": CustomCommand(name="silence")}
    _, footer_legend = _build_legend_bindings(commands)
    assert ("m", "silence") in footer_legend


def test_refresh_stamp_just_now_includes_fetch_duration() -> None:
    assert _refresh_stamp(3.0, 2.84) == "  Refreshed just now \u00b7 2.8s"


def test_refresh_stamp_just_now_without_duration() -> None:
    assert _refresh_stamp(3.0, None) == "  Refreshed just now"


def test_refresh_stamp_ages_and_drops_duration() -> None:
    assert _refresh_stamp(32.0, 2.8) == "  Refreshed 32s ago"
    assert _refresh_stamp(300.0, 2.8) == "  Refreshed 5m ago"
    assert _refresh_stamp(7300.0, 2.8) == "  Refreshed 2h ago"


def test_update_refresh_stamp_noop_before_first_refresh() -> None:
    state = _make_state()
    state.steady_footer_text = "  Loading..."
    _update_refresh_stamp(state)
    assert state.steady_footer_text == "  Loading..."


def test_stamp_tick_updates_footer_and_reschedules() -> None:
    state = _make_state()
    state.last_refresh_time = 1.0
    scheduled: list[float] = []
    loop = SimpleNamespace(set_alarm_in=lambda delay, cb, data: scheduled.append(delay), screen=_MockScreen())
    _on_stamp_tick(cast(Any, loop), state)
    assert state.steady_footer_text.startswith("  Refreshed ")
    assert state.steady_footer_text.endswith(" ago")
    assert scheduled == [10.0]


def test_question_mark_opens_help_overlay_and_any_close_key_restores_board() -> None:
    state = _make_state()
    state.legend_bindings = [("space", "peek"), ("q", "quit"), ("?", "help")]
    state.loop = SimpleNamespace(widget=state.frame, set_alarm_in=_CallTracker(), screen=_MockScreen())
    handler = _KanpanInputHandler(state=state)
    assert handler("?") is True
    assert state.help_overlay is not None
    assert state.loop.widget is state.help_overlay
    assert handler("x") is True
    assert state.help_overlay is not None
    assert handler("esc") is True
    assert state.help_overlay is None
    assert state.loop.widget is state.frame


def test_update_peek_header_names_agent() -> None:
    entry = make_board_entry(name="agent-a", state=AgentLifecycleState.WAITING)
    state = _make_state_with_walker((entry,))
    _build_peek_panel(state)
    state.peek_agent_name = AgentName("agent-a")
    _update_peek_header(state)
    assert "agent-a" in state.peek_box.title_widget.text


def test_update_peek_header_missing_agent_falls_back() -> None:
    state = _make_state_with_walker((make_board_entry(name="agent-a"),))
    _build_peek_panel(state)
    state.peek_agent_name = AgentName("gone")
    _update_peek_header(state)
    assert state.peek_box.title_widget.text.strip() == "Peek"


def test_refresh_display_updates_open_peek_header() -> None:
    entry = make_board_entry(name="agent-a", state=AgentLifecycleState.WAITING)
    state = _make_state(snapshot=make_board_snapshot(entries=(entry,)))
    _build_peek_panel(state)
    state.peek_agent_name = AgentName("agent-a")
    # A completed board refresh re-renders the open panel's title from the new entries.
    _refresh_display(state)
    title = state.peek_box.title_widget.text
    assert "agent-a" in title
    assert str(AgentLifecycleState.WAITING) in title


def test_cancel_peek_alarm_removes_pending_alarm() -> None:
    state = _make_state()
    loop = _RecordingLoop()
    state.loop = cast(Any, loop)
    state.peek_alarm = 7
    _cancel_peek_alarm(state)
    assert state.peek_alarm is None
    assert loop.removed == [7]


def test_on_peek_capture_poll_renders_body_when_done() -> None:
    state = _make_state()
    state.loop = _make_mock_loop()
    state.peek_agent_name = AgentName("agent-a")
    state.peek_body_text = Text("")
    future: Future[subprocess.CompletedProcess[str]] = Future()
    future.set_result(subprocess.CompletedProcess(args=[], returncode=0, stdout="alpha\nbeta\n", stderr=""))
    state.peek_capture_future = future
    _on_peek_capture_poll(state.loop, state)
    assert state.peek_capture_future is None
    assert "beta" in str(state.peek_body_text.text)


def test_on_peek_capture_poll_reschedules_while_running() -> None:
    state = _make_state()
    state.loop = _make_mock_loop()
    state.peek_agent_name = AgentName("agent-a")
    state.peek_body_text = Text("unchanged")
    # A future that never resolves stays "not done".
    state.peek_capture_future = Future()
    _on_peek_capture_poll(state.loop, state)
    # A running capture is left in place and the body is not overwritten.
    assert state.peek_capture_future is not None
    assert str(state.peek_body_text.text) == "unchanged"


def test_handle_peek_key_esc_closes_panel() -> None:
    entry = make_board_entry(name="agent-a")
    state = _make_state_with_walker((entry,))
    original_footer = Text("bar")
    state.frame.footer = original_footer
    state.peek_agent_name = AgentName("agent-a")
    state.saved_footer = original_footer
    state.frame.footer = Text("panel")
    assert _handle_peek_key(state, "esc") is True
    assert state.peek_agent_name is None


def test_handle_peek_key_arrows_are_not_handled() -> None:
    state = _make_state()
    _build_peek_panel(state)
    state.peek_agent_name = AgentName("agent-a")
    # Arrows are not panel actions: the reply Edit already handled in-line cursor
    # movement before this handler runs, so the handler leaves them alone (None)
    # and never attaches or switches the peeked agent.
    for key in ("up", "down", "left", "right"):
        assert _handle_peek_key(state, key) is None
    assert state.peek_agent_name == AgentName("agent-a")


def test_handle_peek_key_unknown_passes_through() -> None:
    state = _make_state()
    state.peek_agent_name = AgentName("agent-a")
    assert _handle_peek_key(state, "z") is None


def test_toggle_peek_opens_panel_for_focused_agent() -> None:
    entry = make_board_entry(name="agent-a")
    state = _make_state_with_walker((entry,))
    original_footer = Text("keybinding-bar")
    state.frame.footer = original_footer
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    _toggle_peek(state)
    # The panel replaces the footer (saved for restore-on-close) and takes key focus.
    assert state.peek_agent_name == AgentName("agent-a")
    assert state.focused_agent_name == AgentName("agent-a")
    assert state.saved_footer is original_footer
    assert state.frame.footer is state.peek_box
    assert state.frame.focus_position == "footer"
    assert str(state.peek_body_text.text) == "(loading...)"


def test_toggle_peek_closes_when_already_open() -> None:
    entry = make_board_entry(name="agent-a")
    state = _make_state_with_walker((entry,))
    original_footer = Text("bar")
    state.frame.footer = original_footer
    state.peek_agent_name = AgentName("agent-a")
    state.saved_footer = original_footer
    state.frame.footer = Text("panel")
    _toggle_peek(state)
    assert state.peek_agent_name is None


def test_submit_peek_reply_empty_input_is_noop() -> None:
    entry = make_board_entry(name="agent-a")
    state = _make_state_with_walker((entry,))
    _build_peek_panel(state)
    state.frame.footer = Text("panel")
    state.peek_agent_name = AgentName("agent-a")
    # An empty reply sends nothing and leaves the panel open (attach is a board action).
    _submit_peek_reply(state)
    assert state.peek_agent_name == AgentName("agent-a")
    assert state.peek_reply_future is None


def _make_reply_result(returncode: int, stderr: str = "") -> Future[subprocess.CompletedProcess[str]]:
    future: Future[subprocess.CompletedProcess[str]] = Future()
    future.set_result(subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr))
    return future


def test_on_peek_reply_poll_failure_drops_echo_and_shows_error() -> None:
    state = _make_state()
    state.loop = _make_mock_loop()
    _build_peek_panel(state)
    state.peek_agent_name = AgentName("agent-a")
    state.peek_pending_replies = ["my reply"]
    future = _make_reply_result(returncode=1, stderr="agent not running\n")
    _on_peek_reply_poll(state.loop, (state, future, AgentName("agent-a"), "my reply"))
    # The optimistic echo is dropped (it will never appear in the transcript) and the
    # failure renders in the panel instead of vanishing silently.
    assert state.peek_pending_replies == []
    assert "reply failed" in str(state.peek_body_text.text)
    assert "agent not running" in str(state.peek_body_text.text)


def test_on_peek_reply_poll_success_keeps_echo() -> None:
    state = _make_state()
    state.loop = _make_mock_loop()
    _build_peek_panel(state)
    state.peek_agent_name = AgentName("agent-a")
    state.peek_pending_replies = ["my reply"]
    future = _make_reply_result(returncode=0)
    _on_peek_reply_poll(state.loop, (state, future, AgentName("agent-a"), "my reply"))
    # A delivered reply keeps its echo until the transcript refresh prunes it.
    assert state.peek_pending_replies == ["my reply"]
    assert state.peek_reply_error == ""


def test_on_peek_reply_poll_failure_after_close_shows_transient() -> None:
    state = _make_state()
    state.loop = _make_mock_loop()
    future = _make_reply_result(returncode=1, stderr="delivery timed out\n")
    _on_peek_reply_poll(state.loop, (state, future, AgentName("agent-a"), "my reply"))
    # Panel closed: the failure goes to the (now visible) footer as a transient message.
    assert state.transient_message is not None
    assert "delivery timed out" in state.transient_message


def test_on_peek_reply_poll_failure_with_other_agent_peeked_renders_in_panel() -> None:
    state = _make_state()
    state.loop = _make_mock_loop()
    _build_peek_panel(state)
    state.peek_agent_name = AgentName("agent-b")
    state.peek_pending_replies = ["draft to b"]
    future = _make_reply_result(returncode=1, stderr="agent not running\n")
    _on_peek_reply_poll(state.loop, (state, future, AgentName("agent-a"), "my reply"))
    # agent-b's panel hides the footer, so the failure renders in the panel body,
    # named so it cannot be misread as agent-b's failure.
    body = str(state.peek_body_text.text)
    assert "reply failed" in body
    assert "agent-a" in body
    assert "agent not running" in body
    assert state.transient_message is None
    # agent-b's own pending echoes are untouched.
    assert state.peek_pending_replies == ["draft to b"]


def test_submit_then_transcript_refresh_keeps_reply_error_visible() -> None:
    state = _make_state()
    state.loop = _make_mock_loop()
    _build_peek_panel(state)
    state.peek_agent_name = AgentName("agent-a")
    state.peek_reply_error = "agent not running"
    # A successful transcript refresh re-renders the body through _set_peek_body, which
    # must keep the failure notice visible rather than wiping it.
    future: Future[subprocess.CompletedProcess[str]] = Future()
    future.set_result(subprocess.CompletedProcess(args=[], returncode=0, stdout="alpha\n", stderr=""))
    state.peek_capture_future = future
    _on_peek_capture_poll(state.loop, state)
    assert "alpha" in str(state.peek_body_text.text)
    assert "reply failed" in str(state.peek_body_text.text)


def test_make_readline_edit_binds_arrow_word_chords() -> None:
    edit = _make_readline_edit(("peek_hint", "reply> "))
    # The library binds Meta+letter word ops; we add the Option/Ctrl+arrow chords.
    assert edit.keymap["meta left"] == edit.backward_word
    assert edit.keymap["ctrl left"] == edit.backward_word
    assert edit.keymap["meta right"] == edit.forward_word
    assert edit.keymap["ctrl right"] == edit.forward_word


def test_make_readline_edit_word_move_and_delete() -> None:
    edit = _make_readline_edit(("peek_hint", "reply> "))
    edit.set_edit_text("hello world foo")
    edit.set_edit_pos(len("hello world foo"))
    edit.keypress((40,), "meta left")
    assert edit.edit_pos == len("hello world ")
    edit.keypress((40,), "ctrl w")
    assert edit.edit_text == "hello foo"


def test_make_readline_edit_defers_enter_and_boundary_left() -> None:
    edit = _make_readline_edit(("peek_hint", "reply> "))
    # Enter and Left-at-column-0 are unhandled, so they bubble to the panel.
    assert edit.keypress((40,), "enter") == "enter"
    assert edit.keypress((40,), "left") == "left"


def test_make_readline_edit_leaves_transpose_unbound() -> None:
    edit = _make_readline_edit(("peek_hint", "reply> "))
    # urwid_readline's transpose misses its one-character guard behind a caption, so it
    # reads past the start of an empty input (raising) and scrambles a short one.
    assert edit.keypress((40,), "ctrl t") == "ctrl t"
    assert edit.get_edit_text() == ""
    edit.set_edit_text("ab")
    edit.set_edit_pos(2)
    assert edit.keypress((40,), "ctrl t") == "ctrl t"
    assert edit.get_edit_text() == "ab"


def test_handle_peek_key_left_falls_through_to_reply_edit() -> None:
    entry = make_board_entry(name="agent-a")
    state = _make_state_with_walker((entry,))
    _build_peek_panel(state)
    state.peek_agent_name = AgentName("agent-a")
    # Left is cursor movement in the reply Edit, never a board return.
    assert _handle_peek_key(state, "left") is None
    assert state.peek_agent_name == AgentName("agent-a")


# =============================================================================
# Prompted commands
# ======================================================================


def _make_prompt_state(
    cmd: CustomCommand,
    key: str = "t",
    agent_name: str = "agent-a",
) -> _KanpanState:
    """A state with one focused agent, `cmd` bound to `key`, and a loop to hold the overlay."""
    entry = make_board_entry(name=agent_name, section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    state.commands = {**state.commands, key: cmd}
    state.frame.footer = Text("keybinding-bar")
    state.loop = SimpleNamespace(widget=state.frame, set_alarm_in=_CallTracker(), screen=_MockScreen())
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName(agent_name))
    state.list_walker.set_focus(agent_idx)
    return state


def _prompt_box(state: _KanpanState) -> Any:
    """The LineBox inside the open prompt's centred overlay."""
    return state.loop.widget.top_w


def _writes_input_to(marker: Path) -> str:
    """A shell command that records MNGR_AGENT_NAME and MNGR_INPUT to `marker`."""
    return f'printf "%s|%s" "$MNGR_AGENT_NAME" "$MNGR_INPUT" > {marker}'


def _drain_prompt_executor(state: _KanpanState) -> None:
    assert state.prompt_executor is not None
    state.prompt_executor.shutdown(wait=True)


def test_run_shell_command_sync_sets_agent_name_and_input_env_vars() -> None:
    result = _run_shell_command_sync('printf "%s|%s" "$MNGR_AGENT_NAME" "$MNGR_INPUT"', "agent-a", "needs review")
    assert result.returncode == 0
    assert result.stdout == "agent-a|needs review"


def test_run_shell_command_sync_does_not_word_split_quoted_input() -> None:
    # The value travels through the environment, never through the shell's parse phase,
    # so a quoted expansion of a multi-word value stays one argument.
    result = _run_shell_command_sync('printf "[%s]" "$MNGR_INPUT"', "agent-a", "two words")
    assert result.stdout == "[two words]"


def test_dispatch_prompted_command_opens_a_centred_overlay_over_the_board() -> None:
    cmd = CustomCommand(name="tag", prompt="tag: ", command="true")
    state = _make_prompt_state(cmd)
    belt = state.frame.footer
    _dispatch_command(state, "t", cmd)
    assert state.open_prompt is not None
    # The overlay floats over the board rather than taking the footer slot, so the
    # keybinding belt is left untouched.
    overlay = state.loop.widget
    assert overlay is not state.frame
    assert overlay.bottom_w is state.frame
    assert state.frame.footer is belt
    assert state.saved_footer is None
    # Nothing has run yet.
    assert state.prompt_executor is None
    assert state.action_label is None


def test_prompt_titles_the_agent_and_leaves_the_ask_to_the_caption() -> None:
    # The caption is the command's own `prompt` text, so repeating the command name in
    # the title just stutters ("tag . agent-a" above "tag: "). Title names the target.
    cmd = CustomCommand(name="tag", prompt="tag: ", command="true")
    state = _make_prompt_state(cmd)
    _dispatch_command(state, "t", cmd)
    assert state.open_prompt is not None
    assert state.open_prompt.edit.caption == "tag: "
    title = _prompt_box(state).title_widget.text
    assert "agent-a" in title
    assert "tag" not in title


def test_prompt_panel_hint_advertises_apply_and_cancel() -> None:
    # Enter/Esc are the prompt's only two actions and the hint is the sole place they
    # are visible, since the keybinding belt is hidden while the prompt is open.
    cmd = CustomCommand(name="tag", prompt="tag: ", command="true")
    state = _make_prompt_state(cmd)
    _dispatch_command(state, "t", cmd)
    hint_text = _prompt_box(state).original_widget.original_widget.contents[1][0].text
    assert "enter" in hint_text
    assert "apply" in hint_text
    assert "esc" in hint_text
    assert "cancel" in hint_text


def test_dispatch_non_prompted_command_runs_immediately() -> None:
    cmd = CustomCommand(name="event", command="true")
    state = _make_prompt_state(cmd)
    _dispatch_command(state, "t", cmd)
    assert state.open_prompt is None
    assert state.executor is not None
    state.executor.shutdown(wait=True)


def test_open_prompt_for_command_without_focused_agent_does_nothing() -> None:
    # Needing a target agent is a rule of the command case, not of the prompt utility.
    cmd = CustomCommand(name="tag", prompt="tag: ", command="true")
    state = _make_state()
    state.frame.footer = Text("keybinding-bar")
    _open_prompt_for_command(state, cmd)
    assert state.open_prompt is None
    assert str(state.frame.footer.text) == "keybinding-bar"


def test_prompt_enter_submits_typed_text_as_mngr_input(tmp_path: Path) -> None:
    marker = tmp_path / "tag-value.txt"
    cmd = CustomCommand(name="tag", prompt="tag: ", command=_writes_input_to(marker))
    state = _make_prompt_state(cmd)
    _dispatch_command(state, "t", cmd)
    assert state.open_prompt is not None
    state.open_prompt.edit.set_edit_text("needs review")
    assert _handle_prompt_key(state, "enter") is True
    _drain_prompt_executor(state)
    assert marker.read_text() == "agent-a|needs review"
    # Submitting closes the prompt before launching, so the footer's progress message shows.
    assert state.open_prompt is None
    assert state.action_label == "  Running tag on agent-a"


def test_prompt_enter_on_empty_line_submits_empty_string(tmp_path: Path) -> None:
    # Clearing a label is `mngr label X -l "tag="`, so an empty line must be deliverable
    # rather than treated as a cancel.
    marker = tmp_path / "cleared-value.txt"
    cmd = CustomCommand(name="tag", prompt="tag: ", command=_writes_input_to(marker))
    state = _make_prompt_state(cmd)
    _dispatch_command(state, "t", cmd)
    assert _handle_prompt_key(state, "enter") is True
    _drain_prompt_executor(state)
    assert marker.read_text() == "agent-a|"


def test_prompt_esc_cancels_without_running(tmp_path: Path) -> None:
    marker = tmp_path / "never-written.txt"
    cmd = CustomCommand(name="tag", prompt="tag: ", command=_writes_input_to(marker))
    state = _make_prompt_state(cmd)
    _dispatch_command(state, "t", cmd)
    assert state.open_prompt is not None
    state.open_prompt.edit.set_edit_text("discarded")
    assert _handle_prompt_key(state, "esc") is True
    assert state.open_prompt is None
    assert state.prompt_executor is None
    assert not marker.exists()
    # Cancelling is silent: no footer message and no in-progress label.
    assert state.transient_message is None
    assert state.action_label is None


def test_prompt_ctrl_c_cancels_rather_than_exiting() -> None:
    cmd = CustomCommand(name="tag", prompt="tag: ", command="true")
    state = _make_prompt_state(cmd)
    handler = _KanpanInputHandler(state=state)
    assert handler("t") is True
    assert state.open_prompt is not None
    # Ctrl-C must not reach the quit branch while the prompt is open.
    assert handler("ctrl c") is True
    assert state.open_prompt is None
    assert state.prompt_executor is None


def test_prompt_gate_swallows_board_keys() -> None:
    cmd = CustomCommand(name="tag", prompt="tag: ", command="true")
    state = _make_prompt_state(cmd)
    handler = _KanpanInputHandler(state=state)
    handler("t")
    assert state.open_prompt is not None
    # Space, quit, a command key, and unmark-all are all board actions that must not
    # fire while the user is typing.
    for key in (" ", "q", "d", "U", "?", "r"):
        handler(key)
    assert state.open_prompt is not None
    assert state.peek_agent_name is None
    assert state.help_overlay is None
    assert state.marks == {}


def test_prompt_board_keys_become_text_and_only_actions_bubble_out() -> None:
    # The gate is only half the routing: keys reach the input through urwid focus, and only
    # the keys the Edit refuses reach the handler at all. Driving the frame exercises the
    # whole Frame -> LineBox -> Pile -> Edit chain, which the handler-level tests skip.
    cmd = CustomCommand(name="tag", prompt="tag: ", command="true")
    state = _make_prompt_state(cmd)
    state.frame.body = ListBox(state.list_walker)
    _dispatch_command(state, "t", cmd)
    assert state.open_prompt is not None
    size = (80, 12)
    overlay = state.loop.widget
    for key in (" ", "q", "d", "U"):
        assert overlay.keypress(size, key) is None
    assert state.open_prompt.edit.get_edit_text() == " qdU"
    for key in ("enter", "esc", "ctrl c"):
        assert overlay.keypress(size, key) == key


def test_close_prompt_hands_the_screen_back_to_the_board() -> None:
    cmd = CustomCommand(name="tag", prompt="tag: ", command="true")
    state = _make_prompt_state(cmd)
    belt = state.frame.footer
    _dispatch_command(state, "t", cmd)
    assert state.loop.widget is not state.frame
    _close_prompt(state)
    assert state.open_prompt is None
    # Leaving the overlay installed would leave the board unable to receive anything.
    assert state.loop.widget is state.frame
    # The prompt floats over the board, so the keybinding belt was never displaced.
    assert state.frame.footer is belt
    assert state.saved_footer is None


def test_close_prompt_when_no_prompt_open_is_noop() -> None:
    state = _make_state()
    state.frame.footer = Text("keybinding-bar")
    _close_prompt(state)
    assert str(state.frame.footer.text) == "keybinding-bar"


def test_submit_prompt_when_no_prompt_open_is_noop() -> None:
    state = _make_state()
    _submit_prompt(state)
    assert state.prompt_executor is None
    assert state.action_label is None


def test_handle_prompt_key_printable_keys_fall_through_to_edit() -> None:
    cmd = CustomCommand(name="tag", prompt="tag: ", command="true")
    state = _make_prompt_state(cmd)
    _dispatch_command(state, "t", cmd)
    # Editing keys are already consumed by the Edit before reaching the handler, so it
    # leaves them alone rather than treating them as prompt actions.
    for key in ("z", "left", "right", "up", "down"):
        assert _handle_prompt_key(state, key) is None
    assert state.open_prompt is not None


def test_open_prompt_captures_target_agent_against_later_focus_moves(tmp_path: Path) -> None:
    # A periodic refresh landing mid-typing moves the board's focus; the command must
    # still run against the agent that was focused when the prompt opened.
    marker = tmp_path / "captured-agent.txt"
    cmd = CustomCommand(name="tag", prompt="tag: ", command=_writes_input_to(marker))
    entry_a = make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    entry_b = make_board_entry(name="agent-b", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry_a, entry_b))
    state.commands = {**state.commands, "t": cmd}
    state.frame.footer = Text("keybinding-bar")
    a_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(a_idx)
    _dispatch_command(state, "t", cmd)
    b_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-b"))
    state.list_walker.set_focus(b_idx)
    _submit_prompt(state)
    _drain_prompt_executor(state)
    assert marker.read_text() == "agent-a|"


def test_prompted_command_uses_its_own_executor_not_the_shared_one() -> None:
    # The shared executor is max_workers=1 and also serves board refreshes, so a prompted
    # write against an unresponsive host must not queue ahead of a refresh.
    cmd = CustomCommand(name="tag", prompt="tag: ", command="true")
    state = _make_prompt_state(cmd)
    state.executor = ThreadPoolExecutor(max_workers=1)
    _dispatch_command(state, "t", cmd)
    _submit_prompt(state)
    assert state.prompt_executor is not None
    assert state.prompt_executor is not state.executor
    state.executor.shutdown(wait=False)
    _drain_prompt_executor(state)


def test_ensure_prompt_executor_is_created_once() -> None:
    state = _make_state()
    executor = _ensure_prompt_executor(state)
    assert _ensure_prompt_executor(state) is executor
    executor.shutdown(wait=False)


def test_binding_description_marks_prompted_command_with_ellipsis() -> None:
    assert _binding_description(CustomCommand(name="tag", prompt="tag: ", command="true")) == "tag…"
    assert _binding_description(CustomCommand(name="event", command="true")) == "event"
    assert _binding_description(_BUILTIN_COMMANDS[_BUILTIN_COMMAND_KEY_REFRESH]) == "refresh"


def test_legend_bindings_mark_prompted_commands_in_overlay_and_footer() -> None:
    commands: dict[str, KanpanCommand] = {
        **_BUILTIN_COMMANDS,
        "t": CustomCommand(name="tag", prompt="tag: ", command="true"),
        _BUILTIN_COMMAND_KEY_MUTE: CustomCommand(name="silence", prompt="why: ", command="true"),
    }
    overlay_bindings, footer_legend = _build_legend_bindings(commands)
    assert ("t", "tag…") in overlay_bindings
    assert (_BUILTIN_COMMAND_KEY_MUTE, "silence…") in footer_legend


def test_load_user_commands_accepts_prompt_with_markable() -> None:
    config = KanpanPluginConfig.model_construct(
        commands={"t": {"name": "tag", "prompt": "tag: ", "command": "true", "markable": True}},
    )
    loaded = _load_user_commands(make_mngr_ctx_with_config(config))
    assert loaded["t"].prompt == "tag: "
    assert loaded["t"].is_markable is True


# =============================================================================
# The prompt as a reusable utility (no custom command involved)
# =============================================================================


class _RecordingPromptHandler:
    """Prompt handler standing in for a future built-in feature's action."""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    def __call__(self, input_text: str) -> None:
        self.submitted.append(input_text)


def _make_state_with_belt() -> _KanpanState:
    """A state with a stand-in keybinding belt and a loop, with no agent focused."""
    state = _make_state()
    state.frame.footer = Text("keybinding-bar")
    state.loop = SimpleNamespace(widget=state.frame, set_alarm_in=_CallTracker(), screen=_MockScreen())
    return state


def test_open_prompt_answers_an_arbitrary_handler_with_no_agent_or_command() -> None:
    # The seam a future built-in feature (interactive filter, jump-to-agent, batch
    # prompting) uses: no focused agent, no CustomCommand, no shell execution.
    state = _make_state_with_belt()
    handler = _RecordingPromptHandler()
    _open_prompt(state, title=" filter ", caption="/", on_submit=handler)
    assert state.open_prompt is not None
    assert state.open_prompt.edit.caption == "/"
    assert _prompt_box(state).title_widget.text.strip() == "filter"
    state.open_prompt.edit.set_edit_text("state == RUNNING")
    _submit_prompt(state)
    assert handler.submitted == ["state == RUNNING"]
    # Answering hands the screen back to the board and never touches the command machinery.
    assert state.open_prompt is None
    assert state.loop.widget is state.frame
    assert state.prompt_executor is None
    assert state.action_label is None


def test_open_prompt_esc_cancels_without_calling_its_handler() -> None:
    state = _make_state_with_belt()
    handler = _RecordingPromptHandler()
    _open_prompt(state, title=" filter ", caption="/", on_submit=handler)
    assert _handle_prompt_key(state, "esc") is True
    assert handler.submitted == []
    assert state.open_prompt is None


def test_open_prompt_refuses_to_stack_over_an_open_prompt() -> None:
    # A second install would overwrite the saved keybinding belt with the first
    # prompt's own panel, leaving the belt unrecoverable on close.
    state = _make_state_with_belt()
    first = _RecordingPromptHandler()
    second = _RecordingPromptHandler()
    _open_prompt(state, title=" first ", caption="1> ", on_submit=first)
    first_overlay = state.loop.widget
    _open_prompt(state, title=" second ", caption="2> ", on_submit=second)
    assert state.loop.widget is first_overlay
    assert state.open_prompt is not None
    assert state.open_prompt.edit.caption == "1> "
    _close_prompt(state)
    assert state.loop.widget is state.frame


# =============================================================================
# Mouse gate: the board must not take clicks while a prompt is open
# =============================================================================


def _make_prompt_state_with_rows() -> _KanpanState:
    """Prompt state whose frame has a real ListBox body, so clicks can hit rows."""
    entries = (
        make_board_entry(name="agent-a", section=BoardSection.STILL_COOKING),
        make_board_entry(name="agent-b", section=BoardSection.STILL_COOKING),
    )
    state = _make_state_with_walker(entries)
    state.frame.body = ListBox(state.list_walker)
    state.frame.footer = Text("keybinding-bar")
    state.list_walker.set_focus(min(state.index_to_entry))
    return state


def test_board_listbox_still_moves_selection_on_a_raw_click_while_prompt_is_open() -> None:
    # Why the gate exists: urwid dispatches mouse events by position, not focus, and
    # ListBox.mouse_event changes focus as a side effect (note it reports unhandled).
    # Filtering has to happen before the widget tree; `unhandled_input` runs too late.
    state = _make_prompt_state_with_rows()
    cmd = CustomCommand(name="tag", prompt="tag: ", command="true")
    _dispatch_command(state, "t", cmd)
    size = (80, 24)
    state.frame.render(size, focus=True)
    state.frame.mouse_event(size, "mouse press", 1, 10, 3, focus=True)
    focused = _get_focused_entry(state)
    assert focused is not None and focused.name == AgentName("agent-b")


def test_mouse_gate_withholds_clicks_while_the_prompt_is_open() -> None:
    state = _make_prompt_state_with_rows()
    gate = _KanpanInputFilter(state=state)
    click: tuple[str, int, int, int] = ("mouse press", 1, 10, 3)
    # Closed prompt: the board owns the mouse as usual.
    assert gate(["a", click], []) == ["a", click]
    _dispatch_command(state, "t", CustomCommand(name="tag", prompt="tag: ", command="true"))
    # Open prompt: the click never reaches the widget tree, but typing still does.
    assert gate(["a", click], []) == ["a"]


def test_mouse_gate_keeps_the_selection_put_while_the_prompt_is_open() -> None:
    # End to end over the real dispatch path: filter first, then hand on what survives,
    # exactly as MainLoop does.
    state = _make_prompt_state_with_rows()
    gate = _KanpanInputFilter(state=state)
    _dispatch_command(state, "t", CustomCommand(name="tag", prompt="tag: ", command="true"))
    size = (80, 24)
    state.frame.render(size, focus=True)
    for key in gate([("mouse press", 1, 10, 3)], []):
        state.frame.mouse_event(size, *key, focus=True)
    focused = _get_focused_entry(state)
    assert focused is not None and focused.name == AgentName("agent-a")


def test_mouse_gate_also_withholds_clicks_for_the_peek_panel_and_help_overlay() -> None:
    # Every modal surface has the same hole: urwid routes the click positionally, so the
    # board takes it while the panel holds the keyboard.
    state = _make_prompt_state_with_rows()
    gate = _KanpanInputFilter(state=state)
    click: tuple[str, int, int, int] = ("mouse press", 1, 10, 3)
    assert not _is_modal_surface_open(state)
    assert gate(["a", click], []) == ["a", click]

    state.peek_agent_name = AgentName("agent-a")
    assert _is_modal_surface_open(state)
    assert gate(["a", click], []) == ["a"]
    state.peek_agent_name = None

    state.help_overlay = object()
    assert _is_modal_surface_open(state)
    assert gate(["a", click], []) == ["a"]
    state.help_overlay = None
    assert gate(["a", click], []) == ["a", click]


def test_peek_panel_keeps_the_selection_put_against_a_click() -> None:
    state = _make_prompt_state_with_rows()
    gate = _KanpanInputFilter(state=state)
    _toggle_peek(state)
    assert state.peek_agent_name == AgentName("agent-a")
    size = (80, 24)
    state.frame.render(size, focus=True)
    for key in gate([("mouse press", 1, 10, 3)], []):
        state.frame.mouse_event(size, *key, focus=True)
    focused = _get_focused_entry(state)
    assert focused is not None and focused.name == AgentName("agent-a")


def test_prompt_overlay_renders_at_narrow_and_wide_terminals() -> None:
    # The box is a fixed width floating over the board; it must not blow up when the
    # terminal is narrower than that width, nor stretch when it is much wider.
    cmd = CustomCommand(name="tag", prompt="tag: ", command="true")
    state = _make_prompt_state(cmd)
    state.frame.body = ListBox(state.list_walker)
    _dispatch_command(state, "t", cmd)
    overlay = state.loop.widget
    for cols, rows in ((200, 40), (100, 24), (60, 16), (40, 12)):
        canvas = overlay.render((cols, rows), focus=True)
        assert canvas.cols() == cols
        assert canvas.rows() == rows
        rendered = [line.decode(errors="replace") for line in canvas.text]
        assert any("tag:" in line for line in rendered), f"caption missing at {cols}x{rows}"
        # Box is bounded, never full-width, whenever the terminal has room for it.
        if cols > 60:
            assert not any(line.strip().startswith("\u256d") and len(line.strip()) >= cols for line in rendered)


# =============================================================================
# _build_agent_row terminal hyperlinks
# =============================================================================

_OSC8_SEQUENCE_PATTERN = re.compile(rb"\x1b\]8;;[^\x1b]*\x1b\\")
_HYPERLINK_CREATED = datetime(2027, 1, 1, 0, 0, 20, tzinfo=timezone.utc)


def _make_prs_def(is_flexible: bool = False) -> _ColumnDef:
    """Build a column def for the PR field, mirroring runtime construction."""
    return _ColumnDef(
        name=FIELD_PR,
        header="PRS",
        text_fn=_FieldCellTextFn(field_key=FIELD_PR),
        markup_fn=_FieldCellMarkupFn(field_key=FIELD_PR),
        flexible=is_flexible,
    )


def _make_multi_pr_field(leading_number: int, *additional: tuple[int, PrState]) -> PrField:
    """A PR field whose cell lists the recorded branch's PR plus additional ones."""
    return make_pr_field(
        number=leading_number,
        additional_prs=tuple(make_additional_pr(number, state) for number, state in additional),
        created=_HYPERLINK_CREATED,
    )


def _render_row(row: Any, width: int) -> tuple[bytes, bytes]:
    """Render a row and return what the terminal receives, with and without OSC 8 escapes."""
    rendered = b"".join(
        text for row_content in row.render((width,)).content() for _attr, _charset, text in row_content
    )
    return rendered, _OSC8_SEQUENCE_PATTERN.sub(b"", rendered)


def _osc8_link(url: str, text: str) -> bytes:
    """The exact byte sequence a terminal reads as `text` hyperlinked to `url`."""
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\".encode()


def test_build_agent_row_links_each_open_pr_number_to_its_own_pr() -> None:
    """Every PR number in the multi-PR cell is separately clickable: each is wrapped
    in its own OSC 8 sequence, and the separator between them is outside both links.
    """
    prs = _make_multi_pr_field(2482, (2392, PrState.OPEN))
    entry = make_board_entry(fields={FIELD_PR: prs}, cells={FIELD_PR: prs.display()})
    column_defs = [*_BUILTIN_COLUMN_DEFS, _make_prs_def()]
    widths = _compute_board_column_widths((entry,), column_defs)

    row = _build_agent_row(entry, widths, column_defs, now=_HYPERLINK_CREATED, staleness_threshold_seconds=1800.0)
    rendered, _visible = _render_row(row, 60)

    assert _osc8_link("https://github.com/org/repo/pull/2482", "#2482") in rendered
    assert _osc8_link("https://github.com/org/repo/pull/2392", "#2392") in rendered
    assert b"\x1b\\, \x1b]8" in rendered


def test_build_agent_row_multi_link_column_width_ignores_the_escape_bytes() -> None:
    """The escape bytes an OSC 8 link injects are invisible, so the column must be
    sized -- and every later column aligned -- by the visible text alone.
    """
    prs = _make_multi_pr_field(2482, (2392, PrState.OPEN))
    linked_entry = make_board_entry(name="agent-a", fields={FIELD_PR: prs}, cells={FIELD_PR: prs.display()})
    plain_entry = make_board_entry(name="agent-b")
    column_defs = [*_BUILTIN_COLUMN_DEFS, _make_prs_def()]
    widths = _compute_board_column_widths((linked_entry, plain_entry), column_defs)
    assert widths[FIELD_PR] == len("#2482, #2392")

    rendered_rows = [
        _render_row(
            _build_agent_row(entry, widths, column_defs, now=_HYPERLINK_CREATED, staleness_threshold_seconds=1800.0),
            60,
        )
        for entry in (linked_entry, plain_entry)
    ]

    linked_rendered, linked_visible = rendered_rows[0]
    _plain_rendered, plain_visible = rendered_rows[1]
    # The linked row carries more bytes than it shows, yet occupies the same columns.
    assert len(linked_rendered) > len(linked_visible)
    assert len(linked_visible) == len(plain_visible) == 60
    assert linked_visible.decode().rstrip() == "  agent-a  RUNNING  #2482, #2392"


def test_build_agent_row_multi_link_cell_in_the_flexible_last_column() -> None:
    """The flexible last column has no fixed width, so its final run absorbs the
    remaining space; the link must still stop at the visible text.
    """
    prs = _make_multi_pr_field(2482, (2392, PrState.OPEN))
    entry = make_board_entry(fields={FIELD_PR: prs}, cells={FIELD_PR: prs.display()})
    column_defs = [*_BUILTIN_COLUMN_DEFS, _make_prs_def(is_flexible=True)]
    widths = _compute_board_column_widths((entry,), column_defs)

    row = _build_agent_row(entry, widths, column_defs, now=_HYPERLINK_CREATED, staleness_threshold_seconds=1800.0)
    rendered, visible = _render_row(row, 60)

    assert _osc8_link("https://github.com/org/repo/pull/2392", "#2392") in rendered
    assert len(visible) == 60


def test_build_agent_row_colors_each_run_by_its_own_state() -> None:
    """A merged or closed PR listed away from its section carries its state in its
    color, and the runs around it keep theirs.
    """
    prs = _make_multi_pr_field(100, (101, PrState.MERGED), (102, PrState.CLOSED))
    entry = make_board_entry(fields={FIELD_PR: prs}, cells={FIELD_PR: prs.display()})
    column_defs = [*_BUILTIN_COLUMN_DEFS, _make_prs_def()]
    widths = _compute_board_column_widths((entry,), column_defs)

    row = _build_agent_row(entry, widths, column_defs, now=_HYPERLINK_CREATED, staleness_threshold_seconds=1800.0)
    rendered_content = [segment for row_content in row.render((60,)).content() for segment in row_content]
    # A linked segment carries its OSC 8 escapes; the visible text is what identifies it.
    visible_by_attr = [
        (_OSC8_SEQUENCE_PATTERN.sub(b"", text).decode(), attr) for attr, _charset, text in rendered_content
    ]
    attr_by_number = {visible: attr for visible, attr in visible_by_attr if visible in ("#100", "#101", "#102")}

    assert attr_by_number == {
        "#100": None,
        "#101": "field_pr_light_magenta",
        "#102": "field_pr_dark_gray",
    }


def test_build_field_color_palette_registers_the_colors_runs_ask_for() -> None:
    """A run's color only renders if the palette carries it, focus variant included."""
    prs = _make_multi_pr_field(100, (101, PrState.MERGED))
    entry = make_board_entry(fields={FIELD_PR: prs}, cells={FIELD_PR: prs.display()})

    palette_entries, attr_names = _build_field_color_palette(make_board_snapshot(entries=(entry,)))

    assert "field_pr_light_magenta" in attr_names
    assert ("field_pr_light_magenta", "light magenta", "") in palette_entries
    assert ("field_pr_light_magenta_focus", "light magenta,standout", "") in palette_entries


def test_build_agent_row_stale_multi_link_cell_greys_every_run() -> None:
    """A stale cell greys out as a whole; splitting it into per-link runs, each with
    its own color, must not leave some of them at full brightness.
    """
    prs = _make_multi_pr_field(2482, (2392, PrState.MERGED))
    entry = make_board_entry(fields={FIELD_PR: prs}, cells={FIELD_PR: prs.display()})
    column_defs = [*_BUILTIN_COLUMN_DEFS, _make_prs_def()]
    widths = _compute_board_column_widths((entry,), column_defs)

    row = _build_agent_row(
        entry,
        widths,
        column_defs,
        now=_HYPERLINK_CREATED + timedelta(seconds=3600),
        staleness_threshold_seconds=1800.0,
    )
    rendered_content = [segment for row_content in row.render((60,)).content() for segment in row_content]

    linked_attrs = {attr for attr, _charset, text in rendered_content if b"#2482" in text or b"#2392" in text}
    assert linked_attrs == {"stale"}


def test_build_agent_row_muted_multi_link_cell_greys_every_run() -> None:
    """Same for a muted row: the whole-row flatten must win over each run's own color
    so the row stays one continuous band of grey.
    """
    prs = _make_multi_pr_field(2482, (2392, PrState.MERGED))
    entry = make_board_entry(section=BoardSection.MUTED, fields={FIELD_PR: prs}, cells={FIELD_PR: prs.display()})
    column_defs = [*_BUILTIN_COLUMN_DEFS, _make_prs_def()]
    widths = _compute_board_column_widths((entry,), column_defs)

    row = _build_agent_row(entry, widths, column_defs, now=_HYPERLINK_CREATED, staleness_threshold_seconds=1800.0)
    rendered_content = [segment for row_content in row.render((60,)).content() for segment in row_content]

    linked_attrs = {attr for attr, _charset, text in rendered_content if b"#2482" in text or b"#2392" in text}
    assert linked_attrs == {"muted"}


def test_build_agent_row_single_url_cell_still_links_the_whole_cell() -> None:
    """A cell carrying one url and no runs -- the single-PR column -- keeps
    hyperlinking its whole text.
    """
    pr = make_pr_field(number=7, created=_HYPERLINK_CREATED)
    entry = make_board_entry(fields={FIELD_PR: pr}, cells={FIELD_PR: pr.display()})
    column_defs = [
        *_BUILTIN_COLUMN_DEFS,
        _ColumnDef(
            name=FIELD_PR,
            header="PR",
            text_fn=_FieldCellTextFn(field_key=FIELD_PR),
            markup_fn=_FieldCellMarkupFn(field_key=FIELD_PR),
            flexible=False,
        ),
    ]
    widths = _compute_board_column_widths((entry,), column_defs)

    row = _build_agent_row(entry, widths, column_defs, now=_HYPERLINK_CREATED, staleness_threshold_seconds=1800.0)
    rendered, _visible = _render_row(row, 60)

    assert _osc8_link("https://github.com/org/repo/pull/7", "#7") in rendered


def test_update_row_mark_with_a_multi_link_cell_ahead_of_the_name_column() -> None:
    """A multi-link cell renders as nested columns rather than a single piece of text,
    so marking a row must find the name cell rather than assume it comes first.
    """
    prs = _make_multi_pr_field(2482, (2392, PrState.OPEN))
    entry = make_board_entry(name="agent-a", fields={FIELD_PR: prs}, cells={FIELD_PR: prs.display()})
    snapshot = make_board_snapshot(entries=(entry,))
    state = _make_state(snapshot=snapshot)
    column_defs = [_make_prs_def(), *_BUILTIN_COLUMN_DEFS]
    walker, idx_map = _build_board_widgets(snapshot, column_defs)
    state.list_walker = walker
    state.index_to_entry = idx_map
    agent_idx = next(k for k, v in idx_map.items() if v.name == AgentName("agent-a"))

    _update_row_mark(state, agent_idx, "d")

    marked_row = walker[agent_idx]
    assert isinstance(marked_row, AttrMap)
    row = marked_row.original_widget
    assert isinstance(row, _SelectableRow)
    assert row.name_cell is not None
    assert row.name_cell.text.startswith("d")


# Search
# =============================================================================

_PR_COLUMN_DEF = _ColumnDef(
    name="pr",
    header="PR",
    text_fn=_FieldCellTextFn(field_key="pr"),
    markup_fn=_FieldCellMarkupFn(field_key="pr"),
    flexible=True,
)


def _make_search_state(entries: tuple[AgentBoardEntry, ...]) -> _KanpanState:
    """State with a real footer Pile and a populated walker, ready for the search prompt."""
    column_defs = [*_BUILTIN_COLUMN_DEFS, _PR_COLUMN_DEF]
    snapshot = make_board_snapshot(entries=entries)
    state = _make_state(snapshot=snapshot)
    state.column_defs = column_defs
    walker, idx_map = _build_board_widgets(snapshot, column_defs)
    footer, footer_belt, footer_right = _build_footer(state.footer_left_attr)
    frame = _BoardFrame(body=ListBox(walker), footer=footer)
    frame.kanpan_state = state
    state.frame = frame
    state.footer_pile = footer
    state.footer_columns = footer_belt
    state.footer_right = footer_right
    state.list_walker = walker
    state.index_to_entry = idx_map
    return state


def _focused_name(state: _KanpanState) -> str | None:
    entry = _get_focused_entry(state)
    return str(entry.name) if entry is not None else None


def _focus_first_agent_row(state: _KanpanState) -> None:
    """Park the board's focus on the first agent row, past the section heading at index 0."""
    state.list_walker.set_focus(min(state.index_to_entry))


def _install_board_legend(state: _KanpanState, bindings: Sequence[tuple[str, str]]) -> None:
    """Give the belt a board legend to paint and to remember for restoring, as run_kanpan does."""
    state.footer_legend = list(bindings)
    _set_footer_legend(state, state.footer_legend)


def _type_query(state: _KanpanState, text: str) -> None:
    """Feed printable keys to the search Edit, as urwid does before unhandled_input."""
    for char in text:
        state.search_input.keypress((40,), char)


def _highlighted_names(state: _KanpanState) -> list[str]:
    """Names of rows currently painted with the focus attributes."""
    return [
        str(entry.name)
        for idx, entry in state.index_to_entry.items()
        if state.list_walker[idx].attr_map.get(None) == "reversed"
    ]


_SEARCH_ENTRIES = (
    make_board_entry(name="lima-host-dir", section=BoardSection.PR_MERGED, cells={"pr": CellDisplay(text="#172")}),
    make_board_entry(name="kanpan-peek", section=BoardSection.PR_MERGED, cells={"pr": CellDisplay(text="#140")}),
    make_board_entry(name="kanpan-search", section=BoardSection.STILL_COOKING, cells={"pr": CellDisplay(text="")}),
    make_board_entry(
        name="release-candidate", section=BoardSection.STILL_COOKING, cells={"pr": CellDisplay(text="#118")}
    ),
)


_ONE_ROW: tuple[tuple[AgentName, str], ...] = ((AgentName("agent-a"), "agent-a RUNNING"),)
_TWO_KANPAN_ROWS: tuple[tuple[AgentName, str], ...] = (
    (AgentName("my-kanpan"), "my-kanpan RUNNING"),
    (AgentName("kanpan-search"), "kanpan-search RUNNING"),
)
_THREE_KANPAN_ROWS: tuple[tuple[AgentName, str], ...] = (
    (AgentName("kanpan-a"), "kanpan-a RUNNING"),
    (AgentName("kanpan-b"), "kanpan-b RUNNING"),
    (AgentName("kanpan-c"), "kanpan-c RUNNING"),
)


@pytest.mark.parametrize(
    ("rows", "query", "expected"),
    (
        pytest.param(_ONE_ROW, "", (), id="empty-query-matches-nothing"),
        pytest.param(_ONE_ROW, "   ", (), id="blank-query-matches-nothing"),
        pytest.param(_ONE_ROW, "zzzz", (), id="no-match"),
        pytest.param(
            _TWO_KANPAN_ROWS,
            "kanpan",
            (AgentName("kanpan-search"), AgentName("my-kanpan")),
            id="name-prefix-beats-name-substring",
        ),
        pytest.param(
            ((AgentName("Kanpan-Search"), "Kanpan-Search RUNNING"),),
            "KANPAN",
            (AgentName("Kanpan-Search"),),
            id="case-insensitive",
        ),
        pytest.param(
            _THREE_KANPAN_ROWS,
            "kanpan",
            (AgentName("kanpan-a"), AgentName("kanpan-b"), AgentName("kanpan-c")),
            id="ties-keep-board-order",
        ),
    ),
)
def test_rank_matches(rows: tuple[tuple[AgentName, str], ...], query: str, expected: tuple[AgentName, ...]) -> None:
    assert _rank_matches(rows, query) == expected


def test_rank_matches_name_beats_other_cells() -> None:
    entries = (
        make_board_entry(name="other", cells={"pr": CellDisplay(text="#2531")}),
        make_board_entry(name="agent-2531", cells={"pr": CellDisplay(text="")}),
    )
    rows = _search_rows(_make_search_state(entries))
    assert _rank_matches(rows, "2531") == (AgentName("agent-2531"), AgentName("other"))


def test_rank_matches_finds_pr_number_in_a_non_name_cell() -> None:
    rows = _search_rows(_make_search_state(_SEARCH_ENTRIES))
    assert _rank_matches(rows, "#140") == (AgentName("kanpan-peek"),)


def test_search_rows_includes_every_column_text() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    rows = dict(_search_rows(state))
    assert "#172" in rows[AgentName("lima-host-dir")]
    assert "lima-host-dir" in rows[AgentName("lima-host-dir")]


def test_search_rows_follows_board_order() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    assert [str(name) for name, _ in _search_rows(state)] == [str(e.name) for e in _SEARCH_ENTRIES]


def test_open_search_takes_over_the_status_slot() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    assert state.search_input is not None
    # The prompt replaces the refresh stamp rather than adding a footer row.
    assert len(state.footer_pile.contents) == 2
    assert state.footer_columns.contents[_FOOTER_STATUS_SLOT][0].original_widget is state.search_input
    assert state.frame.focus_position == "footer"


def test_a_query_wider_than_its_slot_keeps_the_footer_two_rows() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    assert state.footer_pile.rows((_NARROW_SCREEN_COLS,)) == 2
    _type_query(state, "a-very-long-query-that-someone-might-plausibly-type-here")
    # A wrapping query would grow the footer, taking a row from the board mid-search.
    assert state.footer_pile.rows((_NARROW_SCREEN_COLS,)) == 2


def test_close_search_gives_the_status_slot_back() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan")
    _close_search(state, is_cancelled=False)
    assert state.footer_columns.contents[_FOOTER_STATUS_SLOT][0] is state.footer_left_attr
    assert len(state.footer_pile.contents) == 2


def test_open_search_remembers_the_row_to_return_to() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    assert state.pre_search_focus == AgentName("lima-host-dir")


def test_open_search_is_idempotent_while_already_open() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    first_input = state.search_input
    _open_search(state)
    assert state.search_input is first_input
    assert len(state.footer_pile.contents) == 2


def test_typing_focuses_the_best_match() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    _type_query(state, "kanpan")
    assert state.search_matches == (AgentName("kanpan-peek"), AgentName("kanpan-search"))
    assert _focused_name(state) == "kanpan-peek"


def test_typing_a_pr_number_focuses_that_agent() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    _type_query(state, "#118")
    assert _focused_name(state) == "release-candidate"


def test_opening_the_prompt_keeps_the_selected_row_visible() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    # The prompt takes the keyboard, so the ListBox renders unfocused and only an
    # explicit highlight keeps the user's place on the board.
    assert _highlighted_names(state) == ["lima-host-dir"]


def test_opening_the_prompt_on_a_board_with_no_selection_highlights_nothing() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _clear_focus(state)
    _open_search(state)
    assert _highlighted_names(state) == []


def test_search_highlights_only_the_current_match() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan")
    assert _highlighted_names(state) == ["kanpan-peek"]


def test_no_match_leaves_the_selection_where_it_was() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    _type_query(state, "zzzz")
    assert state.search_matches == ()
    assert _focused_name(state) == "lima-host-dir"
    # Still painted, or "the selection stays where it was" would be invisible.
    assert _highlighted_names(state) == ["lima-host-dir"]


def test_cycle_search_wraps_forward_and_back() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan")
    _cycle_search(state, 1)
    assert _focused_name(state) == "kanpan-search"
    _cycle_search(state, 1)
    assert _focused_name(state) == "kanpan-peek"
    _cycle_search(state, -1)
    assert _focused_name(state) == "kanpan-search"


def test_cycle_search_moves_the_highlight() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan")
    _cycle_search(state, 1)
    assert _highlighted_names(state) == ["kanpan-search"]


def test_cycle_search_without_matches_is_a_noop() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    _cycle_search(state, 1)
    assert state.search_index == 0
    assert _focused_name(state) == "lima-host-dir"


def test_close_search_keeps_the_match_focused() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    _type_query(state, "kanpan")
    _close_search(state, is_cancelled=False)
    assert _focused_name(state) == "kanpan-peek"
    assert state.focused_agent_name == AgentName("kanpan-peek")
    assert state.search_input is None
    assert len(state.footer_pile.contents) == 2
    assert state.frame.focus_position == "body"


def test_close_search_cancelled_restores_the_prior_row() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    _type_query(state, "release")
    assert _focused_name(state) == "release-candidate"
    _close_search(state, is_cancelled=True)
    assert _focused_name(state) == "lima-host-dir"
    assert state.focused_agent_name == AgentName("lima-host-dir")


def test_close_search_cancelled_restores_having_selected_nothing() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    # `up` at the top row clears the selection, so the search opens with no row to
    # come back to.
    _focus_first_agent_row(state)
    _clear_focus(state)
    _open_search(state)
    _type_query(state, "kanpan")
    _close_search(state, is_cancelled=True)
    assert _focused_name(state) is None
    assert state.focused_agent_name is None


def test_close_search_forgets_the_prior_row_when_it_commits_no_selection() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    _type_query(state, "zzzz")
    state.snapshot = make_board_snapshot(entries=tuple(e for e in _SEARCH_ENTRIES if str(e.name) != "lima-host-dir"))
    _refresh_display(state)
    _close_search(state, is_cancelled=False)
    # The board shows nothing selected, so a later refresh must not resurrect the row
    # the search opened on.
    assert _focused_name(state) is None
    assert state.focused_agent_name is None


def test_close_search_cancelled_when_the_prior_row_has_left_the_board() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    _type_query(state, "kanpan")
    state.snapshot = make_board_snapshot(entries=tuple(e for e in _SEARCH_ENTRIES if str(e.name) != "lima-host-dir"))
    _refresh_display(state)
    _close_search(state, is_cancelled=True)
    # With nowhere to come back to, cancelling clears -- keeping the match is what
    # committing looks like, and the two gestures must not agree.
    assert _focused_name(state) is None
    assert state.focused_agent_name is None


def test_close_search_clears_the_explicit_highlight() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan")
    _close_search(state, is_cancelled=False)
    assert _highlighted_names(state) == []


def test_close_search_when_not_open_is_a_noop() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _close_search(state, is_cancelled=False)
    # Closing takes the board's focus memory from the focused row; with no prompt open
    # there is nothing to take it for, so the memory stays where the board left it.
    assert state.focused_agent_name is None


def test_handle_search_key_enter_commits() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan")
    assert _handle_search_key(state, "enter") is True
    assert state.search_input is None
    assert state.focused_agent_name == AgentName("kanpan-peek")


def test_handle_search_key_esc_cancels() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    _type_query(state, "kanpan")
    assert _handle_search_key(state, "esc") is True
    assert state.search_input is None
    assert state.focused_agent_name == AgentName("lima-host-dir")


def test_handle_search_key_arrows_cycle() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan")
    assert _handle_search_key(state, "down") is True
    assert _focused_name(state) == "kanpan-search"
    assert _handle_search_key(state, "up") is True
    assert _focused_name(state) == "kanpan-peek"


def test_search_input_supports_readline_editing() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan-search")
    state.search_input.keypress((40,), "ctrl w")
    assert state.search_input.get_edit_text() == "kanpan-"
    assert state.search_matches == (AgentName("kanpan-peek"), AgentName("kanpan-search"))


def test_backspace_retraces_the_keystrokes_that_opened_the_prompt() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    edit = state.search_input
    _type_query(state, "kan")
    assert _focused_name(state) == "kanpan-peek"
    for _ in range(len("kan")):
        edit.keypress((40,), "backspace")
    # Erasing the query rewinds the board but keeps the prompt, exactly as if just opened.
    assert state.search_input is edit
    assert edit.get_edit_text() == ""
    assert _focused_name(state) == "lima-host-dir"
    assert _highlighted_names(state) == ["lima-host-dir"]
    # Three characters typed, so only the fourth backspace takes the `/` as well.
    edit.keypress((40,), "backspace")
    assert state.search_input is None


@pytest.mark.parametrize("backspace_key", ("backspace", "ctrl h"))
def test_backspace_on_an_empty_query_cancels_the_search(backspace_key: str) -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    edit = state.search_input
    edit.keypress((40,), backspace_key)
    assert state.search_input is None
    assert _focused_name(state) == "lima-host-dir"


def test_clearing_the_whole_query_keeps_the_prompt_open() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    edit = state.search_input
    _type_query(state, "kanpan")
    edit.keypress((40,), "ctrl u")
    # ctrl-u clears for a retype; only backspace past the start backs out.
    assert state.search_input is edit
    assert edit.get_edit_text() == ""
    assert _focused_name(state) == "lima-host-dir"


def test_erasing_the_query_rewinds_to_having_selected_nothing() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _clear_focus(state)
    _open_search(state)
    edit = state.search_input
    _type_query(state, "kanpan")
    edit.keypress((40,), "ctrl u")
    # An erased query is indistinguishable from one never typed, including on a board
    # whose selection was cleared before the prompt opened.
    assert _focused_name(state) is None


def test_search_input_leaves_cycling_and_exit_keys_unbound() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    # These must bubble past the Edit to the board as cycle / commit / cancel.
    for key in ("up", "down", "enter", "esc"):
        assert state.search_input.keypress((40,), key) == key


def test_handle_search_key_printable_falls_through_to_the_edit() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    # The focused Edit consumed it long before this handler; None keeps the board off it too.
    assert _handle_search_key(state, "k") is None


def test_input_handler_slash_opens_search() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    state.commands = dict(_BUILTIN_COMMANDS)
    handler = _KanpanInputHandler(state=state)
    assert handler(_BUILTIN_COMMAND_KEY_SEARCH) is True
    assert state.search_input is not None


def test_input_handler_does_not_quit_while_searching() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    state.commands = dict(_BUILTIN_COMMANDS)
    handler = _KanpanInputHandler(state=state)
    _open_search(state)
    # `q` reaches the prompt's query Edit as text instead of raising ExitMainLoop.
    assert handler("q") is None
    assert state.search_input is not None


def test_input_handler_search_does_not_preempt_peek() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    state.commands = dict(_BUILTIN_COMMANDS)
    _build_peek_panel(state)
    state.peek_agent_name = AgentName("kanpan-peek")
    handler = _KanpanInputHandler(state=state)
    # The peek reply input owns `/`, so no search prompt opens behind the panel.
    assert handler(_BUILTIN_COMMAND_KEY_SEARCH) is None
    assert state.search_input is None


def test_dispatch_command_search_role_opens_the_prompt() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    cmd = _BUILTIN_COMMANDS[_BUILTIN_COMMAND_KEY_SEARCH]
    _dispatch_command(state, _BUILTIN_COMMAND_KEY_SEARCH, cmd)
    assert state.search_input is not None


def test_search_is_advertised_in_the_overlay_and_the_footer() -> None:
    commands = _build_command_map(make_mngr_ctx_with_config(KanpanPluginConfig()))
    overlay_bindings, footer_legend = _build_legend_bindings(commands)
    assert (_BUILTIN_COMMAND_KEY_SEARCH, "search") in overlay_bindings
    assert (_BUILTIN_COMMAND_KEY_SEARCH, "search") in footer_legend


def test_a_custom_command_on_slash_takes_the_key_from_search() -> None:
    custom = CustomCommand(name="my-slash", command="echo hi")
    config = KanpanPluginConfig(commands={_BUILTIN_COMMAND_KEY_SEARCH: custom})
    commands = _build_command_map(make_mngr_ctx_with_config(config))
    assert commands[_BUILTIN_COMMAND_KEY_SEARCH].name == "my-slash"
    _overlay_bindings, footer_legend = _build_legend_bindings(commands)
    assert (_BUILTIN_COMMAND_KEY_SEARCH, "my-slash") in footer_legend


def test_disabling_slash_leaves_no_search_binding() -> None:
    config = KanpanPluginConfig(commands={_BUILTIN_COMMAND_KEY_SEARCH: CustomCommand(name="off", enabled=False)})
    commands = _build_command_map(make_mngr_ctx_with_config(config))
    assert _BUILTIN_COMMAND_KEY_SEARCH not in commands
    state = _make_search_state(_SEARCH_ENTRIES)
    state.commands = commands
    # With nothing bound to it, `/` is swallowed by the board like any unknown key.
    assert _KanpanInputHandler(state=state)(_BUILTIN_COMMAND_KEY_SEARCH) is True
    assert state.search_input is None


def test_search_status_shows_the_match_counter() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan")
    assert "1/2" in state.footer_right.get_text()[0]
    _cycle_search(state, 1)
    # The counter is the only thing that tells the user which match they are on.
    assert "2/2" in state.footer_right.get_text()[0]


def test_search_status_reports_no_match() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "zzzz")
    assert "no match" in state.footer_right.get_text()[0]


def test_search_counter_is_empty_for_an_empty_query() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    assert _search_counter_text(state, "") == ""


def test_open_search_swaps_the_board_legend_for_the_prompt_keys() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _install_board_legend(state, [("r", "refresh"), ("q", "quit")])
    _open_search(state)
    belt = state.footer_right.get_text()[0]
    assert "select" in belt
    assert "cancel" in belt
    assert "refresh" not in belt


def test_close_search_restores_the_board_legend() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _install_board_legend(state, [("r", "refresh"), ("q", "quit")])
    _open_search(state)
    _close_search(state, is_cancelled=False)
    belt = state.footer_right.get_text()[0]
    assert "refresh" in belt
    assert "select" not in belt


def test_backing_out_of_the_prompt_restores_the_board_legend() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _install_board_legend(state, [("r", "refresh"), ("q", "quit")])
    _open_search(state)
    edit = state.search_input
    _type_query(state, "kan")
    for _ in range(len("kan") + 1):
        edit.keypress((40,), "backspace")
    assert "refresh" in state.footer_right.get_text()[0]


def test_refresh_display_reapplies_an_open_search() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan")
    _refresh_display(state)
    # The walker is rebuilt wholesale, so the match and its highlight must be re-derived.
    assert state.search_matches == (AgentName("kanpan-peek"), AgentName("kanpan-search"))
    assert _focused_name(state) == "kanpan-peek"
    assert _highlighted_names(state) == ["kanpan-peek"]


def test_refresh_display_keeps_the_match_the_user_cycled_to() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan")
    _cycle_search(state, 1)
    _refresh_display(state)
    # A refresh landing mid-search must not drag the selection back to the first match.
    assert state.search_index == 1
    assert _focused_name(state) == "kanpan-search"
    assert _highlighted_names(state) == ["kanpan-search"]


def test_refresh_display_falls_back_when_the_cycled_match_leaves_the_board() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan")
    _cycle_search(state, 1)
    state.snapshot = make_board_snapshot(entries=tuple(e for e in _SEARCH_ENTRIES if str(e.name) != "kanpan-search"))
    _refresh_display(state)
    assert state.search_index == 0
    assert _focused_name(state) == "kanpan-peek"


def test_refresh_display_that_drops_the_only_match_keeps_the_highlight_honest() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    _type_query(state, "release-cand")
    state.snapshot = make_board_snapshot(
        entries=tuple(e for e in _SEARCH_ENTRIES if str(e.name) != "release-candidate")
    )
    _refresh_display(state)
    # A rebuilt ListBox claims the first selectable row on its next render, so the
    # board must be re-anchored here or `enter` commits to a row that was never
    # highlighted -- the belt meanwhile reading `no match`.
    state.frame.render((_TEST_SCREEN_COLS, _TEST_SCREEN_ROWS), focus=True)
    assert state.search_matches == ()
    assert _highlighted_names(state) == ["lima-host-dir"]
    assert _focused_name(state) == "lima-host-dir"


def test_build_focus_map_covers_mark_and_column_attrs() -> None:
    focus_map = _build_focus_map(("mark_d",), ("field_ci_light_red",))
    assert focus_map[None] == "reversed"
    assert focus_map["mark_d"] == "mark_d_focus"
    assert focus_map["field_ci_light_red"] == "field_ci_light_red_focus"
    assert focus_map["state_running"] == "state_running_focus"


def _make_search_state_with_screen(
    entries: tuple[AgentBoardEntry, ...], cols: int = _TEST_SCREEN_COLS
) -> _KanpanState:
    """Search state whose loop reports a screen ``cols`` wide, which the footer measures itself against."""
    state = _make_search_state(entries)
    state.frame.header = Text("header")
    state.loop = SimpleNamespace(screen=_MockScreen(cols=cols))
    return state


_BOARD_CLICK: tuple[str, int, int, int] = ("mouse press", 1, 10, 5)


def _mouse(state: _KanpanState, event: tuple[str, int, int, int]) -> None:
    """Deliver a mouse event through the real Frame, the way urwid's MainLoop does."""
    name, button, col, row = event
    state.frame.mouse_event((_TEST_SCREEN_COLS, _TEST_SCREEN_ROWS), name, button, col, row, True)


def test_a_board_click_ends_the_search() -> None:
    state = _make_search_state_with_screen(_SEARCH_ENTRIES)
    _open_search(state)
    _type_query(state, "kanpan")
    _mouse(state, _BOARD_CLICK)
    assert state.search_input is None
    assert _highlighted_names(state) == []


def test_a_click_on_the_open_prompt_leaves_the_search_open() -> None:
    state = _make_search_state_with_screen(_SEARCH_ENTRIES)
    _open_search(state)
    footer_rows = state.frame.footer.rows((_TEST_SCREEN_COLS,))
    _mouse(state, ("mouse press", 1, 10, _TEST_SCREEN_ROWS - footer_rows))
    assert state.search_input is not None


@pytest.mark.parametrize("button", (3, 4))
def test_a_right_click_or_scroll_over_the_board_leaves_the_search_open(button: int) -> None:
    state = _make_search_state_with_screen(_SEARCH_ENTRIES)
    _open_search(state)
    # Only a left press moves urwid's Frame focus, so nothing else can steal the keyboard.
    _mouse(state, ("mouse press", button, 10, 5))
    assert state.search_input is not None


def _press(state: _KanpanState, key: str) -> str | None:
    """Drive a key through the real widget tree, the way urwid's MainLoop does.

    Calling ``keypress`` on the prompt directly skips the Frame/Pile/Columns
    routing, which is where focus and selectability bugs actually live.
    """
    return state.frame.keypress((_TEST_SCREEN_COLS, _TEST_SCREEN_ROWS), key)


def test_open_search_makes_the_footer_selectable() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    assert state.footer_pile.selectable() is False
    _open_search(state)
    # Frame.keypress skips an unselectable footer, so the prompt would get no keys.
    assert state.footer_pile.selectable() is True
    _close_search(state, is_cancelled=True)
    assert state.footer_pile.selectable() is False


def test_typing_reaches_the_prompt_through_the_widget_tree() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    for char in "kan":
        assert _press(state, char) is None
    assert state.search_input.get_edit_text() == "kan"
    assert _focused_name(state) == "kanpan-peek"


def test_backspace_through_the_widget_tree_backs_out_of_the_prompt() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _focus_first_agent_row(state)
    _open_search(state)
    for char in "kan":
        _press(state, char)
    for _ in range(len("kan")):
        _press(state, "backspace")
    assert state.search_input is not None
    _press(state, "backspace")
    assert state.search_input is None
    assert _focused_name(state) == "lima-host-dir"


def test_arrows_reach_the_board_through_the_widget_tree() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    for char in "kan":
        _press(state, char)
    # up/down must stay unhandled by the prompt so the board can cycle matches.
    assert _press(state, "down") == "down"
    assert _press(state, "enter") == "enter"


def test_transpose_leaves_the_freshly_opened_prompt_alone() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    _open_search(state)
    # An empty query is the state `/` opens in, and the transpose urwid_readline binds to
    # ctrl-t raises on it -- through the widget tree that takes the whole board down.
    assert _press(state, "ctrl t") == "ctrl t"
    assert state.search_input.get_edit_text() == ""


def _process_input(
    state: _KanpanState, handler: _KanpanInputHandler, keys: Sequence[str | tuple[str, int, int, int]]
) -> None:
    """Drive a batch of input the way urwid's MainLoop does: filter it, then walk it in order."""
    for key in _KanpanInputFilter(state=state)(list(keys), []):
        if isinstance(key, tuple):
            _mouse(state, key)
            continue
        unhandled = _press(state, key)
        if unhandled is not None:
            handler(unhandled)


@pytest.mark.parametrize("board_key", ("q", "d"))
def test_a_key_typed_before_a_click_in_its_batch_still_reaches_the_query(board_key: str) -> None:
    state = _make_search_state_with_screen(_SEARCH_ENTRIES)
    state.commands = dict(_BUILTIN_COMMANDS)
    _focus_first_agent_row(state)
    _open_search(state)
    # urwid hands over a whole read of input at once, so a key and the click that follows
    # it arrive together. Ending the search before the click's turn would give the board
    # `q` (quitting kanpan) and `d` (marking a row for delete) instead of the query.
    _process_input(state, _KanpanInputHandler(state=state), [board_key, _BOARD_CLICK])
    assert state.marks == {}
    assert state.search_input is None


# =============================================================================
# Legend fitting
# =============================================================================

_WIDE_LEGEND: tuple[tuple[str, str], ...] = (
    ("/", "search"),
    ("r", "refresh"),
    ("m", "mute"),
    ("d", "mark delete"),
    ("x", "execute"),
    ("q", "quit"),
    ("?", "more keys"),
)


def _belt_text(state: _KanpanState) -> str:
    """Belt text with the legend's non-breaking spaces normalised back for assertions."""
    return str(state.footer_right.get_text()[0]).replace("\u00a0", " ")


def test_legend_width_spaces_bindings_the_way_the_belt_does() -> None:
    refresh = (("r", "refresh"),)
    quit_key = (("q", "quit"),)
    assert _legend_width(refresh + quit_key) == _legend_width(refresh) + _legend_width(quit_key) + len(
        _LEGEND_SEPARATOR
    )
    assert _legend_width(()) == 0


def test_legend_width_counts_a_wide_character_as_two_columns() -> None:
    # A custom command may be named anything, so the legend must be measured in
    # terminal columns; counting code points would let the belt overflow and clip.
    assert _legend_width((("c", "看板"),)) == _legend_width((("c", "abcd"),))
    assert _legend_width((("c", "看板"),)) > len("c") + len(": 看板")


def test_footer_legend_yields_room_to_a_wide_status_text() -> None:
    state = _make_search_state_with_screen(_SEARCH_ENTRIES, cols=60)
    state.footer_left_text.set_text("  Running mute on abcde")
    _set_footer_legend(state, _WIDE_LEGEND)
    assert "execute" in _belt_text(state)
    # A status of the same character count but twice the columns; measuring it in
    # code points would leave the legend a binding too wide for the belt.
    state.footer_left_text.set_text("  Running mute on 看板看板看")
    _set_footer_legend(state, _WIDE_LEGEND)
    assert "execute" not in _belt_text(state)
    assert "more keys" in _belt_text(state)


@pytest.mark.parametrize(
    ("available_cols", "expected"),
    (
        pytest.param(_legend_width(_WIDE_LEGEND), _WIDE_LEGEND, id="everything-fits"),
        # A binding is shown whole or not at all; a clipped one would read as "resh".
        pytest.param(_legend_width(_WIDE_LEGEND) - 1, _WIDE_LEGEND[1:], id="one-column-short-drops-a-whole-binding"),
        # Dropping from the left is why `?` is listed last: it is how the rest are found.
        pytest.param(_legend_width(_WIDE_LEGEND[-1:]), (("?", "more keys"),), id="only-the-tail-fits"),
        pytest.param(0, (), id="nothing-fits"),
    ),
)
def test_fit_legend(available_cols: int, expected: tuple[tuple[str, str], ...]) -> None:
    assert _fit_legend(_WIDE_LEGEND, available_cols) == expected


def test_footer_legend_measures_a_multiline_status_as_the_slot_packs_it() -> None:
    state = _make_search_state_with_screen(_SEARCH_ENTRIES, cols=80)
    widest_line = "  push failed: bad ref"
    state.footer_left_text.set_text(widest_line)
    _set_footer_legend(state, _WIDE_LEGEND)
    one_line_belt = _belt_text(state)
    assert "mark delete" in one_line_belt
    # A failed custom command puts the tool's raw stderr in the status, which is
    # routinely several lines. The slot packs to the widest of them, so the extra
    # lines must cost the legend nothing; counted end to end they would drop
    # bindings against a width the status never takes.
    status = f"{widest_line}\nhint: try again\nhint: or not"
    state.footer_left_text.set_text(status)
    _set_footer_legend(state, _WIDE_LEGEND)
    assert _packed_width(status) == state.footer_left_text.pack()[0]
    assert _belt_text(state) == one_line_belt


def test_footer_legend_shrinks_on_a_narrow_screen() -> None:
    state = _make_search_state_with_screen(_SEARCH_ENTRIES, cols=40)
    state.footer_left_text.set_text("  Refreshed 3m ago")
    _set_footer_legend(state, _WIDE_LEGEND)
    belt = _belt_text(state)
    assert "more keys" in belt
    assert "mark delete" not in belt


def test_footer_legend_is_whole_on_a_wide_screen() -> None:
    state = _make_search_state_with_screen(_SEARCH_ENTRIES, cols=200)
    state.footer_left_text.set_text("  Refreshed 3m ago")
    _set_footer_legend(state, _WIDE_LEGEND)
    belt = _belt_text(state)
    for _key, description in _WIDE_LEGEND:
        assert description in belt


def test_footer_legend_refits_when_the_status_text_grows() -> None:
    state = _make_search_state_with_screen(_SEARCH_ENTRIES, cols=80)
    _set_footer_legend(state, _WIDE_LEGEND)
    state.steady_footer_text = "  Refreshed just now"
    _render_footer(state)
    assert "mark delete" in _belt_text(state)
    state.steady_footer_text = "  Marked: 3 mark delete  (x to execute, U to unmark all)"
    _render_footer(state)
    # A longer status leaves the legend less room, so whole bindings drop out.
    assert "mark delete" not in _belt_text(state)
    assert "more keys" in _belt_text(state)


def test_rendering_the_footer_mid_search_keeps_the_prompt_belt() -> None:
    state = _make_search_state_with_screen(_SEARCH_ENTRIES)
    _install_board_legend(state, _WIDE_LEGEND)
    _open_search(state)
    _type_query(state, "kanpan")
    # The stamp tick, transient messages and every refresh go through _render_footer,
    # which re-fits the belt; none of them may repaint the board's keys over the
    # prompt's, nor drop the counter that says which match the user is on.
    state.steady_footer_text = "  Refreshed 3m ago"
    _render_footer(state)
    belt = _belt_text(state)
    assert "1/2" in belt
    assert "select" in belt
    assert "refresh" not in belt


def test_closing_the_prompt_refits_the_legend_to_the_status_text() -> None:
    state = _make_search_state_with_screen(_SEARCH_ENTRIES, cols=80)
    state.footer_left_text.set_text("  Marked: 3 mark delete  (x to execute, U to unmark all)")
    _install_board_legend(state, _WIDE_LEGEND)
    before = _belt_text(state)
    _open_search(state)
    _close_search(state, is_cancelled=True)
    # The restored legend is fitted around the status text, not around the room the
    # prompt reserved; too wide a belt lands in a narrower slot and clips mid-binding.
    assert _belt_text(state) == before


def test_the_open_prompt_takes_belt_room_for_the_query() -> None:
    # Wide enough that the prompt's own legend fits whole beside the status text it
    # replaced, and narrow enough that it does not once the query is reserved for.
    state = _make_search_state_with_screen(_SEARCH_ENTRIES, cols=56)
    _install_board_legend(state, _WIDE_LEGEND)
    _open_search(state)
    # A belt fitted to what it replaced leaves the query a slot too narrow to read.
    assert "next" not in _belt_text(state)
    query_cols = state.footer_columns.column_widths((56,))[_FOOTER_STATUS_SLOT]
    assert query_cols >= _SEARCH_QUERY_MIN_COLS


def test_input_filter_refits_the_legend_on_a_window_resize() -> None:
    state = _make_search_state(_SEARCH_ENTRIES)
    screen = _MockScreen(cols=200)
    state.loop = SimpleNamespace(screen=screen)
    state.steady_footer_text = "  Refreshed 3m ago"
    _render_footer(state)
    _set_footer_legend(state, _WIDE_LEGEND)
    assert "search" in _belt_text(state)
    input_filter = _KanpanInputFilter(state=state)
    screen.cols = 55
    input_filter(["window resize"], [])
    # Nothing else re-measures the belt, so a resize the filter drops leaves it
    # fitted to the old terminal and clipping mid-binding at the new width.
    assert "search" not in _belt_text(state)
    assert "more keys" in _belt_text(state)
    screen.cols = 200
    input_filter(["window resize"], [])
    assert "search" in _belt_text(state)


# =============================================================================
# Batch prompting: mark N agents, answer once, apply to all
# =============================================================================


def _writes_input_per_agent(directory: Path) -> str:
    """A command that records its MNGR_INPUT under a per-agent filename."""
    return f'printf "%s" "$MNGR_INPUT" > {directory}/"$MNGR_AGENT_NAME".txt'


class _ImmediateExecutor(ThreadPoolExecutor):
    """Executor that runs the work inline, so a batch advances without real waiting.

    A batch is watched by an alarm that collects whichever futures are done. Resolving them
    on submission makes the first poll conclusive, so `_PumpLoop` walks the whole batch
    deterministically.
    """

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        # A real executor hands a failure back through the future rather than raising at
        # the submit site. The board refresh that finishing a batch queues is the one
        # failure expected here: it reaches for a live context this stub does not carry.
        try:
            future.set_result(fn(*args, **kwargs))
        except AttributeError as error:
            future.set_exception(error)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        super().shutdown(wait=wait, cancel_futures=cancel_futures)


class _PumpLoop:
    """Loop stub that queues alarms instead of firing them, so a test can drain them."""

    def __init__(self, widget: Any) -> None:
        self.widget = widget
        self.screen = _MockScreen()
        self.pending: list[tuple[Any, Any]] = []

    def set_alarm_in(self, _delay: float, callback: Any, data: Any = None) -> int:
        self.pending.append((callback, data))
        return len(self.pending)

    def remove_alarm(self, _handle: Any) -> None:
        pass

    def drain_batch(self, state: _KanpanState, limit: int = 100) -> None:
        """Fire queued alarms until the batch reports itself finished.

        Stops there rather than draining everything: finishing queues a board refresh,
        which has no live context here and would reschedule itself forever.
        """
        for _step in range(limit):
            if not state.executing or not self.pending:
                return
            callback, data = self.pending.pop(0)
            callback(self, data)
        raise AssertionError("batch did not settle")


def _make_batch_state(commands: dict[str, CustomCommand], marks: dict[str, str]) -> _KanpanState:
    state = _make_state(commands=commands)
    state.loop = _PumpLoop(state.frame)
    state.batch_executor = _ImmediateExecutor()
    state.frame.footer = Text("keybinding-bar")
    state.marks = {AgentName(name): key for name, key in marks.items()}
    return state


def test_batch_prompt_asks_once_and_applies_the_answer_to_every_marked_agent(tmp_path: Path) -> None:
    cmd = CustomCommand(name="message", prompt="message: ", command=_writes_input_per_agent(tmp_path), markable=True)
    state = _make_batch_state({"M": cmd}, {"agent-a": "M", "agent-b": "M", "agent-c": "M"})
    _execute_marks(state)
    # One prompt for the command, not one per agent, and it names how many it covers.
    assert state.open_prompt is not None
    assert "3 marked" in state.loop.widget.top_w.title_widget.text
    assert state.executing is False
    state.open_prompt.edit.set_edit_text("ship it")
    _submit_prompt(state)
    state.loop.drain_batch(state)
    for name in ("agent-a", "agent-b", "agent-c"):
        assert (tmp_path / f"{name}.txt").read_text() == "ship it"


def test_batch_prompt_cancelled_runs_nothing_and_keeps_the_marks(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    cmd = CustomCommand(name="message", prompt="message: ", command=_writes_input_per_agent(out_dir), markable=True)
    state = _make_batch_state({"M": cmd}, {"agent-a": "M", "agent-b": "M"})
    _execute_marks(state)
    assert state.open_prompt is not None
    state.open_prompt.edit.set_edit_text("discarded")
    _close_prompt(state)
    # A half-applied batch would be worse than none: nothing ran, and the marks survive
    # so the same set can be executed again.
    assert state.executing is False
    assert list(out_dir.iterdir()) == []
    assert set(state.marks) == {AgentName("agent-a"), AgentName("agent-b")}


def test_batch_prompts_once_per_marked_command_and_keeps_the_answers_apart(tmp_path: Path) -> None:
    tag_dir, note_dir = tmp_path / "tag", tmp_path / "note"
    tag_dir.mkdir()
    note_dir.mkdir()
    commands: dict[str, CustomCommand] = {
        "T": CustomCommand(name="tag", prompt="tag: ", command=_writes_input_per_agent(tag_dir), markable=True),
        "N": CustomCommand(name="note", prompt="note: ", command=_writes_input_per_agent(note_dir), markable=True),
    }
    state = _make_batch_state(commands, {"agent-a": "T", "agent-b": "N", "agent-c": "T"})
    assert _prompted_mark_keys(state) == ("T", "N")
    _execute_marks(state)
    assert state.open_prompt is not None
    state.open_prompt.edit.set_edit_text("blocked")
    _submit_prompt(state)
    # Answering the first opens the second rather than starting the batch early.
    assert state.executing is False
    assert state.open_prompt is not None
    state.open_prompt.edit.set_edit_text("looks good")
    _submit_prompt(state)
    state.loop.drain_batch(state)
    assert (tag_dir / "agent-a.txt").read_text() == "blocked"
    assert (tag_dir / "agent-c.txt").read_text() == "blocked"
    assert (note_dir / "agent-b.txt").read_text() == "looks good"


def test_batch_execution_without_a_prompted_command_still_runs_straight_through(tmp_path: Path) -> None:
    # No regression for plain markable commands: no prompt, and MNGR_INPUT is empty.
    cmd = CustomCommand(name="stamp", command=_writes_input_per_agent(tmp_path), markable=True)
    state = _make_batch_state({"S": cmd}, {"agent-a": "S"})
    _execute_marks(state)
    assert state.open_prompt is None
    state.loop.drain_batch(state)
    assert (tmp_path / "agent-a.txt").read_text() == ""


def test_execute_marks_will_not_stack_a_second_prompt_over_a_collection_round() -> None:
    cmd = CustomCommand(name="message", prompt="message: ", command="true", markable=True)
    state = _make_batch_state({"M": cmd}, {"agent-a": "M"})
    _execute_marks(state)
    first_prompt = state.open_prompt
    assert first_prompt is not None
    # A second `x` would strand the first prompt behind the second.
    _execute_marks(state)
    assert state.open_prompt is first_prompt
    assert state.executing is False


# =============================================================================
# Refresh: the view stays put, and the outcome arrives with the rows it describes
# =============================================================================


def _tall_board_state(count: int = 40) -> _KanpanState:
    """A board taller than the screen, so scrolling is real."""
    entries = tuple(make_board_entry(name=f"agent-{i:02d}") for i in range(count))
    state = _make_state(snapshot=make_board_snapshot(entries=entries))
    state.loop = SimpleNamespace(widget=state.frame, set_alarm_in=_CallTracker(), screen=_MockScreen(cols=80, rows=20))
    state.frame.footer = Text("keybinding-bar")
    _refresh_display(state)
    return state


def test_refresh_keeps_the_focused_row_at_the_same_height() -> None:
    # A fresh ListBox starts scrolled to the top and moving the walker's focus does not
    # tell it where to draw, so without the offset restore the view jumps on every
    # refresh -- including the periodic one, under a cursor nobody touched.
    state = _tall_board_state()
    size = _board_body_size(state)
    assert size is not None
    late_index = max(state.index_to_entry)
    state.list_walker.set_focus(late_index)
    state.frame.body.change_focus(size, late_index, offset_inset=4)
    state.frame.body.render(size, focus=True)
    before = state.frame.body.get_focus_offset_inset(size)

    _refresh_display(state)

    state.frame.body.render(size, focus=True)
    assert state.frame.body.get_focus_offset_inset(size) == before
    focused = _get_focused_entry(state)
    assert focused is not None and focused.name == AgentName(f"agent-{len(state.index_to_entry) - 1:02d}")


def test_refreshed_board_shows_the_same_rows_it_showed_before() -> None:
    state = _tall_board_state()
    size = _board_body_size(state)
    assert size is not None
    state.list_walker.set_focus(max(state.index_to_entry))
    state.frame.body.change_focus(size, max(state.index_to_entry), offset_inset=4)
    before = [line.decode(errors="replace") for line in state.frame.body.render(size, focus=True).text]
    _refresh_display(state)
    after = [line.decode(errors="replace") for line in state.frame.body.render(size, focus=True).text]
    assert after == before


def test_a_repainting_command_reports_only_once_the_board_has_repainted() -> None:
    # The outcome and the rows it describes should land together, not a second apart.
    state = _tall_board_state()
    _report_after_refresh(state, "  note completed for agent-00", state.loop)
    # Held while the refresh is in flight, with the in-progress label still up.
    assert state.pending_completion_message == "  note completed for agent-00"
    assert state.transient_message is None
    _flush_pending_completion(state)
    assert state.transient_message == "  note completed for agent-00"
    assert state.pending_completion_message is None
    assert state.action_label is None


def test_a_held_outcome_waits_rather_than_reporting_twice_when_a_refresh_is_running() -> None:
    # `_start_local_refresh` early-returns while one is in flight. The outcome must ride
    # that refresh out rather than being shown now and again when it lands.
    state = _tall_board_state()
    state.refresh_future = cast(Any, object())
    _report_after_refresh(state, "  note completed for agent-00", state.loop)
    assert state.transient_message is None
    assert state.pending_completion_message == "  note completed for agent-00"


def test_a_failed_refresh_still_releases_the_outcome_it_was_carrying() -> None:
    # The flush sits past `_finish_refresh`'s try/finally, so a refresh that raised does
    # not strand the message behind it.
    state = _tall_board_state()
    failed: Future[Any] = Future()
    failed.set_exception(RuntimeError("discovery is down"))
    state.refresh_future = failed
    state.refresh_is_local_only = True
    state.pending_completion_message = "  note completed for agent-00"
    _finish_refresh(cast(Any, state.loop), state)
    assert state.transient_message == "  note completed for agent-00"
    assert state.pending_completion_message is None


# =============================================================================
# Batch execution overlaps independent agents
# =============================================================================


class _RecordingExecutor(ThreadPoolExecutor):
    """Executor that banks what it was handed and returns a future that never resolves."""

    def __init__(self) -> None:
        super().__init__(max_workers=1)
        self.submitted_args: list[tuple[Any, ...]] = []

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Future[Any]:
        self.submitted_args.append(args)
        return Future()


def test_batch_submits_every_operation_before_collecting_any() -> None:
    # Marked agents are independent, so the whole batch goes out at once and the pool's
    # worker count is the only thing limiting overlap. None of these futures ever resolve,
    # so anything that waited for one operation before starting the next would stop at the
    # first agent.
    state = _make_state()
    state.executing = True
    state.loop = _make_mock_loop()
    executor = _RecordingExecutor()
    state.batch_executor = executor
    cmd = CustomCommand(name="message", command="true", markable=True)
    work = [_BatchWorkItem(name=AgentName(name), key="M", cmd=cmd, entry=None) for name in ("a", "b", "c")]

    _execute_batch(state, work)

    assert [args[1] for args in executor.submitted_args] == ["a", "b", "c"]
    assert state.action_label == "  Executing 0/3"
    executor.shutdown(wait=False)


def test_batch_progress_opens_with_an_unrunnable_item_already_counted() -> None:
    # An item that yields no future is finished the moment the batch starts, and every
    # later frame counts it as such, so the opening frame must too.
    state = _make_state()
    state.executing = True
    state.loop = _make_mock_loop()
    executor = _RecordingExecutor()
    state.batch_executor = executor
    runnable = CustomCommand(name="message", command="true", markable=True)
    work = [
        _BatchWorkItem(name=AgentName("a"), key="M", cmd=runnable, entry=None),
        _BatchWorkItem(name=AgentName("b"), key="M", cmd=CustomCommand(name="noop"), entry=None),
    ]

    _execute_batch(state, work)

    assert state.action_label == "  Executing 1/2"
    executor.shutdown(wait=False)


def test_the_batch_pool_runs_as_many_at_once_as_configured() -> None:
    # A barrier only trips if all three are genuinely in flight together; with a pool of
    # one, the first would wait for partners that never arrive.
    state = _make_state()
    state.batch_concurrency = 3
    executor = _ensure_batch_executor(state)
    barrier = threading.Barrier(3, timeout=5)

    def _blocks_until_partners_arrive() -> int:
        return barrier.wait()

    futures = [executor.submit(_blocks_until_partners_arrive) for _ in range(3)]
    assert sorted(f.result(timeout=5) for f in futures) == [0, 1, 2]
    executor.shutdown(wait=True)


def test_batch_concurrency_is_configurable_and_defaults_to_overlapping() -> None:
    assert KanpanPluginConfig().batch_concurrency == 4
    assert KanpanPluginConfig(batch_concurrency=1).batch_concurrency == 1
    with pytest.raises(ValidationError):
        KanpanPluginConfig(batch_concurrency=0)


def test_batch_executor_is_kept_off_the_shared_refresh_executor() -> None:
    # A long batch on the shared max_workers=1 executor would sit in front of the next
    # board refresh.
    state = _make_state()
    state.executor = ThreadPoolExecutor(max_workers=1)
    batch = _ensure_batch_executor(state)
    assert batch is not state.executor
    assert _ensure_batch_executor(state) is batch
    state.executor.shutdown(wait=False)
    batch.shutdown(wait=False)


def test_batch_progress_counts_finished_work_rather_than_naming_one_item() -> None:
    # With several in flight at once no single item stands for the batch, so the label
    # reports how many are done.
    state = _make_state()
    state.loop = _make_mock_loop()
    done = _make_done_future(_completed(0))
    running: Future[subprocess.CompletedProcess[str]] = Future()
    _on_batch_poll(state.loop, (state, [(_batch_item("a"), done), (_batch_item("b"), running)], [], 2))
    assert state.action_label == "  Executing 1/2"
    running.cancel()


def test_quitting_drops_batch_work_that_has_not_started() -> None:
    # Quitting abandons the batch. Leaving its queue full would hold the process open past
    # the teardown, since the interpreter joins thread-pool workers at exit.
    state = _make_state()
    state.batch_concurrency = 1
    executor = _ensure_batch_executor(state)
    has_started = threading.Event()
    may_finish = threading.Event()

    def _occupies_the_only_worker() -> None:
        has_started.set()
        assert may_finish.wait(timeout=5)

    running = executor.submit(_occupies_the_only_worker)
    assert has_started.wait(timeout=5)
    queued = executor.submit(_occupies_the_only_worker)

    _shutdown_executors(state)

    assert queued.cancelled()
    may_finish.set()
    running.result(timeout=5)


# =============================================================================
# The board holds its place even when the focused row is the one that went away
# =============================================================================


def test_refresh_holds_the_view_when_the_focused_row_is_deleted() -> None:
    # Executing a delete mark removes the row the board was anchored to. Without a
    # replacement anchor a rebuilt ListBox renders from the top, so the board jumps --
    # exactly when the user is watching to see the delete land.
    state = _tall_board_state()
    size = _board_body_size(state)
    assert size is not None
    # A row in the middle: deleting the last one legitimately pulls the view up, since
    # there is then less content below to scroll through.
    agent_rows = sorted(state.index_to_entry)
    doomed_index = agent_rows[len(agent_rows) // 2]
    state.list_walker.set_focus(doomed_index)
    state.frame.body.change_focus(size, doomed_index, offset_inset=4)
    top_before = state.frame.body.render(size, focus=True).text[0].decode(errors="replace").strip()
    doomed = _get_focused_entry(state)
    assert doomed is not None

    assert state.snapshot is not None
    survivors = tuple(e for e in state.snapshot.entries if e.name != doomed.name)
    state.snapshot = make_board_snapshot(entries=survivors)
    _refresh_display(state)

    top_after = state.frame.body.render(size, focus=True).text[0].decode(errors="replace").strip()
    assert top_after == top_before
    # Selection lands on a surviving neighbour rather than nothing or the top of the board.
    focused = _get_focused_entry(state)
    assert focused is not None and focused.name != doomed.name


def test_nearest_surviving_name_prefers_the_row_below() -> None:
    order = [AgentName(f"agent-{i}") for i in range(5)]
    present = {AgentName("agent-0"), AgentName("agent-3")}
    # agent-2 is gone: agent-3 (below) wins over agent-0 (further above).
    assert _nearest_surviving_name(order, AgentName("agent-2"), present) == AgentName("agent-3")
    # With nothing below, it walks back up.
    assert _nearest_surviving_name(order, AgentName("agent-4"), {AgentName("agent-1")}) == AgentName("agent-1")
    # Nothing left to anchor to at all.
    assert _nearest_surviving_name(order, AgentName("agent-2"), set()) is None
    # A name that was never on the board cannot be located.
    assert _nearest_surviving_name(order, AgentName("stranger"), present) is None
