"""Unit tests for the kanpan TUI."""

import subprocess
import threading
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import uuid4

import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError
from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.widget.attr_map import AttrMap
from urwid.widget.filler import Filler
from urwid.widget.frame import Frame
from urwid.widget.text import Text

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import FIELD_CI
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_sources.git_info import CommitsAheadField
from imbue.mngr_kanpan.data_sources.github import CiField
from imbue.mngr_kanpan.data_sources.github import CiStatus
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
from imbue.mngr_kanpan.testing import make_board_snapshot
from imbue.mngr_kanpan.testing import make_mngr_ctx_with_config
from imbue.mngr_kanpan.testing import make_pr_field
from imbue.mngr_kanpan.tui import BOARD_SECTION_ORDER
from imbue.mngr_kanpan.tui import _BUILTIN_COLUMN_DEFS
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_DELETE
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_EXECUTE
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_PUSH
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_REFRESH
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_UNMARK
from imbue.mngr_kanpan.tui import _BatchItemResult
from imbue.mngr_kanpan.tui import _BatchWorkItem
from imbue.mngr_kanpan.tui import _ColumnDef
from imbue.mngr_kanpan.tui import _FieldCellMarkupFn
from imbue.mngr_kanpan.tui import _FieldCellTextFn
from imbue.mngr_kanpan.tui import _KanpanInputHandler
from imbue.mngr_kanpan.tui import _KanpanState
from imbue.mngr_kanpan.tui import _assemble_column_defs
from imbue.mngr_kanpan.tui import _batch_item_label
from imbue.mngr_kanpan.tui import _build_agent_row
from imbue.mngr_kanpan.tui import _build_board_widgets
from imbue.mngr_kanpan.tui import _build_command_map
from imbue.mngr_kanpan.tui import _build_data_source_column_defs
from imbue.mngr_kanpan.tui import _build_field_color_palette
from imbue.mngr_kanpan.tui import _build_mark_palette
from imbue.mngr_kanpan.tui import _carry_forward_fields
from imbue.mngr_kanpan.tui import _clear_focus
from imbue.mngr_kanpan.tui import _compute_board_column_widths
from imbue.mngr_kanpan.tui import _compute_footer_display
from imbue.mngr_kanpan.tui import _dispatch_command
from imbue.mngr_kanpan.tui import _execute_marks
from imbue.mngr_kanpan.tui import _execute_next_in_batch
from imbue.mngr_kanpan.tui import _field_cell_markup
from imbue.mngr_kanpan.tui import _field_cell_text
from imbue.mngr_kanpan.tui import _finish_batch_execution
from imbue.mngr_kanpan.tui import _flatten_markup_to_attr
from imbue.mngr_kanpan.tui import _format_section_heading
from imbue.mngr_kanpan.tui import _get_focused_entry
from imbue.mngr_kanpan.tui import _get_name_cell_markup
from imbue.mngr_kanpan.tui import _get_state_attr
from imbue.mngr_kanpan.tui import _is_field_stale
from imbue.mngr_kanpan.tui import _is_focus_on_first_selectable
from imbue.mngr_kanpan.tui import _load_user_commands
from imbue.mngr_kanpan.tui import _on_batch_item_poll
from imbue.mngr_kanpan.tui import _on_transient_expire
from imbue.mngr_kanpan.tui import _prune_orphaned_marks
from imbue.mngr_kanpan.tui import _refresh_display
from imbue.mngr_kanpan.tui import _render_footer
from imbue.mngr_kanpan.tui import _resolve_section_order
from imbue.mngr_kanpan.tui import _run_shell_command
from imbue.mngr_kanpan.tui import _show_transient_message
from imbue.mngr_kanpan.tui import _submit_batch_item
from imbue.mngr_kanpan.tui import _toggle_mark
from imbue.mngr_kanpan.tui import _unmark_all
from imbue.mngr_kanpan.tui import _unmark_focused
from imbue.mngr_kanpan.tui import _update_mark_count_footer
from imbue.mngr_kanpan.tui import _update_row_mark
from imbue.mngr_kanpan.tui import _update_snapshot_mute
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


def _make_mock_loop() -> Any:
    tracker = _CallTracker()
    return SimpleNamespace(set_alarm_in=tracker, _alarm_tracker=tracker)


def _make_entry(
    name: str = "test-agent",
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    branch: str | None = None,
    is_muted: bool = False,
    section: BoardSection = BoardSection.STILL_COOKING,
    fields: dict[str, FieldValue] | None = None,
    cells: dict[str, CellDisplay] | None = None,
) -> AgentBoardEntry:
    return AgentBoardEntry(
        name=AgentName(name),
        state=state,
        provider_name=ProviderInstanceName("local"),
        branch=branch,
        is_muted=is_muted,
        section=section,
        fields=fields or {},
        cells=cells or {},
    )


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
        commands=commands or {},
        column_defs=list(_BUILTIN_COLUMN_DEFS),
        marks={},
        executing=False,
        execute_status="",
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


@pytest.mark.parametrize("state", list(AgentLifecycleState))
def test_get_state_attr_only_running_and_waiting_are_colored(state: AgentLifecycleState) -> None:
    """Only RUNNING and WAITING get a non-empty color attr; every other state falls through to ''."""
    entry = _make_entry(state=state)
    attr = _get_state_attr(entry)
    if state == AgentLifecycleState.RUNNING:
        assert attr == "state_running"
    elif state == AgentLifecycleState.WAITING:
        assert attr == "state_attention"
    else:
        assert attr == ""


def test_get_name_cell_markup_no_mark() -> None:
    entry = _make_entry()
    markup = _get_name_cell_markup(entry)
    assert markup == "  test-agent"


def test_get_name_cell_markup_with_mark() -> None:
    entry = _make_entry()
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
    texts = [w.get_text()[0] for w in walker if isinstance(w, Text)]
    assert "No agents found." in texts


def test_build_board_widgets_one_entry() -> None:
    entry = _make_entry(name="solo-agent", section=BoardSection.STILL_COOKING)
    snapshot = make_board_snapshot(entries=(entry,))
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert len(idx_map) == 1
    idx = next(iter(idx_map))
    text, _attribs = _name_cell_text_and_attrs(walker, idx)
    assert text == "  solo-agent"
    headings = _extract_section_headings(walker)
    assert len(headings) == 1
    assert "In progress" in headings[0]


def test_build_board_widgets_errors_displayed() -> None:
    snapshot = make_board_snapshot(entries=(), errors=("Error 1",))
    walker, _ = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    texts = [w.text if hasattr(w, "text") else "" for w in walker]
    found_error = any("Error 1" in str(t) for t in texts)
    assert found_error


def _section_heading_index(walker: Any, predicate: Any) -> int:
    """Return the walker index of the first section-heading Text matching predicate."""
    for idx, widget in enumerate(walker):
        if isinstance(widget, Text):
            text = widget.get_text()[0]
            if " (" in text and predicate(text):
                return idx
    raise AssertionError("no matching section heading found")


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
    e1 = _make_entry(name="cooking-agent", section=BoardSection.STILL_COOKING)
    e2 = _make_entry(name="merged-agent", section=BoardSection.PR_MERGED)
    snapshot = make_board_snapshot(entries=(e1, e2))
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert len(idx_map) == 2
    # Display order is most-mature first: PR_MERGED ("Done") then STILL_COOKING ("In progress").
    done_idx = _section_heading_index(walker, lambda t: "Done" in t)
    progress_idx = _section_heading_index(walker, lambda t: "In progress" in t)
    assert done_idx < progress_idx
    # Each entry's name cell must land under the heading for its section.
    merged_walker_idx = next(i for i, e in idx_map.items() if e.name == AgentName("merged-agent"))
    cooking_walker_idx = next(i for i, e in idx_map.items() if e.name == AgentName("cooking-agent"))
    assert done_idx < merged_walker_idx < progress_idx
    assert progress_idx < cooking_walker_idx
    assert _name_cell_text_and_attrs(walker, merged_walker_idx)[0] == "  merged-agent"
    assert _name_cell_text_and_attrs(walker, cooking_walker_idx)[0] == "  cooking-agent"


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


def test_update_snapshot_mute() -> None:
    entry = _make_entry(is_muted=False)
    state = _make_state(snapshot=make_board_snapshot(entries=(entry,)))
    _update_snapshot_mute(state, AgentName("test-agent"), True)
    assert state.snapshot is not None
    assert state.snapshot.entries[0].is_muted is True


def test_prune_orphaned_marks() -> None:
    entry = _make_entry(name="agent-a")
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
    entry = _make_entry(cells={"ci": CellDisplay(text="failure", color="light red")})
    assert _field_cell_text(entry, "ci") == "failure"


def test_field_cell_text_absent() -> None:
    entry = _make_entry()
    assert _field_cell_text(entry, "ci") == ""


def test_field_cell_markup_with_color() -> None:
    entry = _make_entry(cells={"ci": CellDisplay(text="failure", color="light red")})
    markup = _field_cell_markup(entry, "ci")
    assert isinstance(markup, tuple)
    assert markup[1] == "failure"


def test_field_cell_markup_no_color() -> None:
    entry = _make_entry(cells={"pr": CellDisplay(text="#42")})
    markup = _field_cell_markup(entry, "pr")
    assert markup == "#42"


def test_field_cell_markup_absent() -> None:
    entry = _make_entry()
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
    entry = _make_entry(cells={"ci": CellDisplay(text="failure", color="light red")})
    snapshot = make_board_snapshot(entries=(entry,))
    entries, names = _build_field_color_palette(snapshot)
    assert len(entries) == 2
    assert "field_ci_light_red" in names


def test_build_field_color_palette_no_colors() -> None:
    entry = _make_entry(cells={"pr": CellDisplay(text="#42")})
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


def _row_at(walker: Any, idx: int) -> Any:
    """Return the `_SelectableRow` at the given walker index (unwrapping AttrMap)."""
    widget = walker[idx]
    return widget.original_widget if isinstance(widget, AttrMap) else widget


def _name_cell_text_and_attrs(walker: Any, idx: int) -> tuple[str, list[tuple[Any, int]]]:
    """Return (plain text, run-length attribs) of the name cell at the given walker index.

    The name cell is always the first column of a row built by `_build_agent_row`.
    """
    name_widget = _row_at(walker, idx).contents[0][0]
    text, attribs = name_widget.get_text()
    return text, attribs


def _name_cell_attr_names(walker: Any, idx: int) -> set[str]:
    """Return the set of attribute names applied to the name cell at the given index."""
    _, attribs = _name_cell_text_and_attrs(walker, idx)
    return {attr for attr, _length in attribs if attr is not None}


def test_build_agent_row_stale_field_uses_stale_attr() -> None:
    now = datetime(2027, 1, 1, 0, 0, 4, tzinfo=timezone.utc)
    ci = CiField(status=CiStatus.FAILURE, created=now - timedelta(seconds=3600))
    entry = _make_entry(
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
    entry = _make_entry(
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
    entry = _make_entry(
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
    old_entry = _make_entry(
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
    new_entry = _make_entry(
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


def test_carry_forward_fields_new_agent() -> None:
    new_entry = _make_entry(name="new-agent")
    old_snapshot = make_board_snapshot(entries=())
    new_snapshot = make_board_snapshot(entries=(new_entry,))
    result = _carry_forward_fields(old_snapshot, new_snapshot)
    assert len(result.entries) == 1
    assert result.entries[0].name == AgentName("new-agent")


# =============================================================================
# _FieldCellTextFn, _FieldCellMarkupFn
# =============================================================================


def test_field_cell_text_fn_call() -> None:
    entry = _make_entry(cells={"pr": CellDisplay(text="#1")})
    fn = _FieldCellTextFn(field_key="pr")
    assert fn(entry) == "#1"


def test_field_cell_markup_fn_call() -> None:
    entry = _make_entry(cells={"pr": CellDisplay(text="#1")})
    fn = _FieldCellMarkupFn(field_key="pr")
    assert fn(entry) == "#1"


# =============================================================================
# CI field markup - color is always provided by CiField.display()
# =============================================================================


def test_field_cell_markup_ci_failure_uses_color_attr() -> None:
    """CI FAILURE cell has color='light red', so markup uses field_ci_light_red attr."""
    ci = CiField(status=CiStatus.FAILURE, created=datetime(2027, 1, 1, 0, 0, 13, tzinfo=timezone.utc))
    cell = ci.display()
    entry = _make_entry(
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
    entry = _make_entry(
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
    entry = _make_entry(
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
    entry = _make_entry(name="a-long-agent-name-here")
    widths = _compute_board_column_widths((entry,), _BUILTIN_COLUMN_DEFS)
    # "  a-long-agent-name-here" is longer than "  NAME"
    assert widths["name"] > len("  NAME")


# =============================================================================
# _build_board_widgets with marks and muted entries
# =============================================================================


def test_build_board_widgets_with_marks() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    snapshot = make_board_snapshot(entries=(entry,))
    marks = {AgentName("agent-a"): "d"}
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS, marks=marks, mark_attr_names=("mark_d",))
    assert len(idx_map) == 1
    idx = next(iter(idx_map))
    # The mark glyph "d" renders in the name cell with the "mark_d" attribute,
    # followed by the agent name with a single leading space.
    text, _attribs = _name_cell_text_and_attrs(walker, idx)
    assert text == "d agent-a"
    assert "mark_d" in _name_cell_attr_names(walker, idx)


def test_build_board_widgets_muted_entry() -> None:
    entry = _make_entry(name="muted-agent", is_muted=True, section=BoardSection.MUTED)
    snapshot = make_board_snapshot(entries=(entry,))
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert len(idx_map) == 1
    idx = next(iter(idx_map))
    text, _attribs = _name_cell_text_and_attrs(walker, idx)
    assert text == "  muted-agent"
    assert _name_cell_attr_names(walker, idx) == {"muted"}


def test_build_board_widgets_multiple_sections() -> None:
    e1 = _make_entry(name="cooking-agent", section=BoardSection.STILL_COOKING)
    e2 = _make_entry(name="review-agent", section=BoardSection.PR_BEING_REVIEWED)
    e3 = _make_entry(name="failed-agent", section=BoardSection.PRS_FAILED)
    snapshot = make_board_snapshot(entries=(e1, e2, e3))
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert len(idx_map) == 3
    # PR_BEING_REVIEWED ("In review") is more mature than the two "In progress"
    # sections (PRS_FAILED and STILL_COOKING share the "In progress" prefix).
    headings = _extract_section_headings(walker)
    assert headings[0].startswith("In review")
    assert all("In progress" in h for h in headings[1:])
    review_idx = next(i for i, e in idx_map.items() if e.name == AgentName("review-agent"))
    assert _name_cell_text_and_attrs(walker, review_idx)[0] == "  review-agent"


# =============================================================================
# _update_row_mark
# =============================================================================


def test_update_row_mark_no_walker() -> None:
    state = _make_state()
    state.marks = {AgentName("agent-a"): "d"}
    # With no walker this is an early-return guard: state must be unchanged.
    _update_row_mark(state, 0, "p")
    assert state.marks == {AgentName("agent-a"): "d"}


def test_update_row_mark_no_entry_at_index() -> None:
    state = _make_state()
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    snapshot = make_board_snapshot(entries=(entry,))
    state.snapshot = snapshot
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    state.list_walker = walker
    state.index_to_entry = idx_map
    # Index 0 is the header row, not an agent entry: the guard returns early and
    # leaves the actual agent row's name cell untouched.
    agent_idx = next(k for k, v in idx_map.items() if v.name == AgentName("agent-a"))
    before, _ = _name_cell_text_and_attrs(walker, agent_idx)
    _update_row_mark(state, 0, "d")
    after, _ = _name_cell_text_and_attrs(walker, agent_idx)
    assert before == after == "  agent-a"


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
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    # Find the index of the agent entry
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    _toggle_mark(state, "d")
    assert AgentName("agent-a") in state.marks
    assert state.marks[AgentName("agent-a")] == "d"
    # The focused row's name cell renders the mark glyph with the mark_d attribute.
    text, _attribs = _name_cell_text_and_attrs(state.list_walker, agent_idx)
    assert text == "d agent-a"
    assert "mark_d" in _name_cell_attr_names(state.list_walker, agent_idx)
    # The footer reflects the single delete mark.
    assert state.footer_left_text.text == "  Marked: 1 delete  (x to execute, U to unmark all)"


def test_toggle_mark_removes_existing_mark() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    state.marks[AgentName("agent-a")] = "d"
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    _toggle_mark(state, "d")
    assert AgentName("agent-a") not in state.marks


def test_toggle_mark_no_walker() -> None:
    # No walker means the early-return guard fires: marks must stay empty.
    state = _make_state()
    _toggle_mark(state, "d")
    assert state.marks == {}


# =============================================================================
# _unmark_focused
# =============================================================================


def test_unmark_focused_removes_mark() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    state.marks[AgentName("agent-a")] = "d"
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    _unmark_focused(state)
    assert AgentName("agent-a") not in state.marks


def test_unmark_focused_no_mark_is_noop() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    state.marks = {AgentName("other"): "d"}
    _unmark_focused(state)
    # The focused agent had no mark, so the unrelated mark is left intact.
    assert state.marks == {AgentName("other"): "d"}
    assert "mark_d" not in _name_cell_attr_names(state.list_walker, agent_idx)


# =============================================================================
# _unmark_all
# =============================================================================


def test_unmark_all_clears_marks() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    state.marks[AgentName("agent-a")] = "d"
    _unmark_all(state)
    assert state.marks == {}


def test_unmark_all_empty_marks_noop() -> None:
    state = _make_state()
    state.steady_footer_text = "  Steady"
    _unmark_all(state)
    # Early return on empty marks: the footer is not touched (no "Marked:" text).
    assert state.marks == {}
    assert state.footer_left_text.text == "  Loading..."


# =============================================================================
# _update_mark_count_footer
# =============================================================================


def test_update_mark_count_footer_with_marks() -> None:
    commands = {"d": CustomCommand(name="delete", markable="light red")}
    state = _make_state(commands=commands)
    state.marks = {AgentName("agent-a"): "d", AgentName("agent-b"): "d"}
    _update_mark_count_footer(state)
    assert state.footer_left_text.text == "  Marked: 2 delete  (x to execute, U to unmark all)"


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
    # With no marks, no batch is started: no executor is created and we stay idle.
    assert state.executing is False
    assert state.executor is None


def test_execute_marks_already_executing_does_nothing() -> None:
    state = _make_state()
    state.marks = {AgentName("a"): "d"}
    state.executing = True
    existing_executor = ThreadPoolExecutor(max_workers=1)
    state.executor = existing_executor
    _execute_marks(state)
    # A batch is already running, so no new batch is started: the executor that
    # was already attached is left untouched.
    assert state.executor is existing_executor
    assert state.executing is True
    existing_executor.shutdown(wait=False)


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
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    commands: dict[str, KanpanCommand] = {"d": CustomCommand(name="delete", markable="light red")}
    state = _make_state_with_walker((entry,))
    state.commands = commands
    state.mark_attr_names = ("mark_d",)
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    _dispatch_command(state, "d", commands["d"])
    assert AgentName("agent-a") in state.marks
    assert "mark_d" in _name_cell_attr_names(state.list_walker, agent_idx)
    assert state.footer_left_text.text == "  Marked: 1 delete  (x to execute, U to unmark all)"


def test_dispatch_command_unmark_key_removes_mark() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
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
    assert state.executor is not None
    state.executor.shutdown(wait=True)
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
    assert state.executor is not None
    state.executor.shutdown(wait=True)
    assert marker.exists()


# =============================================================================
# _refresh_display
# =============================================================================


def test_refresh_display_updates_walker() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    snapshot = make_board_snapshot(entries=(entry,))
    state = _make_state(snapshot=snapshot)
    _refresh_display(state)
    assert state.list_walker is not None
    assert len(state.index_to_entry) == 1
    idx = next(iter(state.index_to_entry))
    assert _name_cell_text_and_attrs(state.list_walker, idx)[0] == "  agent-a"


def test_refresh_display_restores_focus() -> None:
    # Build a multi-entry board, focus a non-first agent, refresh, and assert the
    # focus lands back on that same agent (exercising the focus-restoration loop).
    entries = (
        _make_entry(name="first-agent", section=BoardSection.STILL_COOKING),
        _make_entry(name="second-agent", section=BoardSection.STILL_COOKING),
    )
    snapshot = make_board_snapshot(entries=entries)
    state = _make_state(snapshot=snapshot)
    state.focused_agent_name = AgentName("second-agent")
    _refresh_display(state)
    assert state.list_walker is not None
    _focused_widget, focus_idx = state.list_walker.get_focus()
    assert state.index_to_entry[focus_idx].name == AgentName("second-agent")


def test_refresh_display_none_snapshot() -> None:
    state = _make_state()
    state.snapshot = None
    _refresh_display(state)
    assert state.list_walker is not None
    texts = [w.get_text()[0] for w in state.list_walker if isinstance(w, Text)]
    assert texts == ["Loading..."]


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
    # When snapshot is None, the early-return guard leaves it None.
    state = _make_state()
    state.snapshot = None
    _update_snapshot_mute(state, AgentName("agent"), True)
    assert state.snapshot is None


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
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    state.marks = {AgentName("agent-a"): "d"}
    handler = _KanpanInputHandler(state=state)
    result = handler("U")
    assert result is True
    assert state.marks == {}


def test_input_handler_command_key_dispatches() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    handler = _KanpanInputHandler(state=state)
    result = handler("d")
    assert result is True
    assert AgentName("agent-a") in state.marks
    assert "mark_d" in _name_cell_attr_names(state.list_walker, agent_idx)
    assert state.footer_left_text.text == "  Marked: 1 delete  (x to execute, U to unmark all)"


def test_input_handler_up_key_not_first_passes_through() -> None:
    entry1 = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    entry2 = _make_entry(name="agent-b", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry1, entry2))
    b_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-b"))
    state.list_walker.set_focus(b_idx)
    handler = _KanpanInputHandler(state=state)
    result = handler("up")
    assert result is None


def test_input_handler_up_key_on_first_clears_focus() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
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
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    a_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(a_idx)
    assert _is_focus_on_first_selectable(state) is True


def test_is_focus_on_first_selectable_at_non_first() -> None:
    entry1 = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    entry2 = _make_entry(name="agent-b", section=BoardSection.STILL_COOKING)
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
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
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
    entry = _make_entry(name="muted-agent", is_muted=True, section=BoardSection.MUTED)
    snapshot = make_board_snapshot(entries=(entry,))
    state = _make_state(snapshot=snapshot)
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    state.list_walker = walker
    state.index_to_entry = idx_map
    agent_idx = next(k for k, v in idx_map.items() if v.name == AgentName("muted-agent"))
    _update_row_mark(state, agent_idx, "d")
    # A muted row stays uniformly grey: the mark is flattened into the "muted"
    # attr rather than rendered as a colored "mark_d" glyph.
    text, _attribs = _name_cell_text_and_attrs(walker, agent_idx)
    assert text == "d muted-agent"
    assert _name_cell_attr_names(walker, agent_idx) == {"muted"}


# =============================================================================
# _toggle_mark: push with no work_dir
# =============================================================================


def test_toggle_mark_push_no_work_dir_shows_message() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
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
    assert state.footer_left_text.text == "  Executed 2 operation(s) successfully"
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
    assert state.footer_left_text.text == "  Executed: 1 ok, 1 failed (see errors below)"
    # The failure detail is persisted for rendering at the bottom of the board.
    assert state.execute_errors == ("op2: boom",)


def test_finish_batch_execution_empty_results() -> None:
    state = _make_state()
    state.executing = True
    _finish_batch_execution(state, [])
    assert state.executing is False
    assert state.execute_errors == ()


# =============================================================================
# _on_batch_item_poll
# =============================================================================


def _make_done_future(result: subprocess.CompletedProcess[str]) -> "Future[subprocess.CompletedProcess[str]]":
    """Create an already-completed future with a given result."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut: Future[subprocess.CompletedProcess[str]] = pool.submit(lambda: result)
        fut.result()
    return fut


def test_on_batch_item_poll_future_done_success() -> None:
    state = _make_state()
    state.executing = True
    item = _BatchWorkItem(
        name=AgentName("agent-a"),
        key="c",
        cmd=CustomCommand(name="custom"),
        entry=None,
    )
    state.marks = {AgentName("agent-a"): "c"}
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    future = _make_done_future(proc)
    mock_loop = _make_mock_loop()
    _on_batch_item_poll(mock_loop, (state, future, [item], [], 0, item))
    assert state.executing is False
    assert AgentName("agent-a") not in state.marks


def test_on_batch_item_poll_future_done_failure() -> None:
    state = _make_state()
    state.executing = True
    item = _BatchWorkItem(
        name=AgentName("agent-a"),
        key="c",
        cmd=CustomCommand(name="custom"),
        entry=None,
    )
    state.marks = {AgentName("agent-a"): "c"}
    proc = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="something bad")
    future = _make_done_future(proc)
    mock_loop = _make_mock_loop()
    results: list[_BatchItemResult] = []
    _on_batch_item_poll(mock_loop, (state, future, [item], results, 0, item))
    assert len(results) == 1
    assert results[0].is_success is False
    # The captured stderr is preserved as the failure detail.
    assert results[0].detail == "something bad"


def test_on_batch_item_poll_timeout_reports_clear_detail() -> None:
    state = _make_state()
    state.executing = True
    item = _BatchWorkItem(
        name=AgentName("agent-a"),
        key="c",
        cmd=CustomCommand(name="custom"),
        entry=None,
    )

    def _raise_timeout() -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["mngr", "destroy"], timeout=60)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future: Future[subprocess.CompletedProcess[str]] = pool.submit(_raise_timeout)
        future.exception()
        mock_loop = _make_mock_loop()
        results: list[_BatchItemResult] = []
        _on_batch_item_poll(mock_loop, (state, future, [item], results, 0, item))
    assert len(results) == 1
    assert results[0].is_success is False
    assert results[0].detail == "timed out after 60s"


def test_on_batch_item_poll_future_done_batch_names() -> None:
    state = _make_state()
    state.executing = True
    item = _BatchWorkItem(
        name=AgentName("a"),
        key=_BUILTIN_COMMAND_KEY_DELETE,
        cmd=CustomCommand(name="delete"),
        entry=None,
        batch_names=(AgentName("a"), AgentName("b")),
    )
    state.marks = {AgentName("a"): _BUILTIN_COMMAND_KEY_DELETE, AgentName("b"): _BUILTIN_COMMAND_KEY_DELETE}
    proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    future = _make_done_future(proc)
    mock_loop = _make_mock_loop()
    results: list[_BatchItemResult] = []
    _on_batch_item_poll(mock_loop, (state, future, [item], results, 0, item))
    assert AgentName("a") not in state.marks
    assert AgentName("b") not in state.marks


def test_on_batch_item_poll_future_not_done() -> None:
    state = _make_state()
    state.executing = True
    item = _BatchWorkItem(
        name=AgentName("agent-a"),
        key="c",
        cmd=CustomCommand(name="custom"),
        entry=None,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        barrier = threading.Barrier(2)

        def _wait() -> subprocess.CompletedProcess[str]:
            barrier.wait()
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        future: Future[subprocess.CompletedProcess[str]] = pool.submit(_wait)
        mock_loop = _make_mock_loop()
        _on_batch_item_poll(mock_loop, (state, future, [item], [], 0, item))
        assert mock_loop._alarm_tracker.call_count >= 1
        barrier.wait()


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
    entry = _make_entry(name="agent-a")
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


def test_run_shell_command_submits_future(tmp_path: Path) -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    a_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(a_idx)
    # The command touches a unique marker file; after the executor drains we
    # assert the file exists, proving the command actually ran (not merely that
    # an executor was attached).
    marker = tmp_path / f"ran-{uuid4().hex}"
    assert not marker.exists()
    cmd = CustomCommand(name="say-hi", command=f"touch {marker}")
    _run_shell_command(state, cmd)
    assert state.executor is not None
    state.executor.shutdown(wait=True)
    assert marker.exists()


# =============================================================================
# _execute_next_in_batch: skipped item (future is None)
# =============================================================================


def test_execute_next_in_batch_skipped_item() -> None:
    state = _make_state()
    state.executor = ThreadPoolExecutor(max_workers=1)
    item = _BatchWorkItem(
        name=AgentName("agent-a"),
        key="c",
        cmd=CustomCommand(name="noop"),
        entry=None,
    )
    results: list[_BatchItemResult] = []
    _execute_next_in_batch(state, [item], results, 0)
    assert any("skipped" in r.detail for r in results)
    state.executor.shutdown(wait=False)


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
        _make_entry(name="cooking"),
        _make_entry(name="merged", section=BoardSection.PR_MERGED),
    )
    walker, _ = _build_board_widgets(make_board_snapshot(entries=entries), _BUILTIN_COLUMN_DEFS)
    headings = _extract_section_headings(walker)
    assert len(headings) == 2
    assert "Done" in headings[0]
    assert "In progress" in headings[1]


def test_build_board_widgets_custom_section_order_reverses() -> None:
    entries = (
        _make_entry(name="cooking"),
        _make_entry(name="merged", section=BoardSection.PR_MERGED),
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
        _make_entry(name="cooking"),
        _make_entry(name="merged", section=BoardSection.PR_MERGED),
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
