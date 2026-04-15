"""Unit tests for the kanpan TUI."""

import subprocess
import threading
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
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
from imbue.mngr_kanpan.data_sources.github import PrField
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import CustomCommand
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.tui import BOARD_SECTION_ORDER
from imbue.mngr_kanpan.tui import _BUILTIN_COLUMN_DEFS
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_DELETE
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_EXECUTE
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_PUSH
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_REFRESH
from imbue.mngr_kanpan.tui import _BUILTIN_COMMAND_KEY_UNMARK
from imbue.mngr_kanpan.tui import _BatchWorkItem
from imbue.mngr_kanpan.tui import _FieldCellMarkupFn
from imbue.mngr_kanpan.tui import _FieldCellTextFn
from imbue.mngr_kanpan.tui import _KanpanInputHandler
from imbue.mngr_kanpan.tui import _KanpanState
from imbue.mngr_kanpan.tui import _assemble_column_defs
from imbue.mngr_kanpan.tui import _batch_item_label
from imbue.mngr_kanpan.tui import _build_board_widgets
from imbue.mngr_kanpan.tui import _build_command_map
from imbue.mngr_kanpan.tui import _build_data_source_column_defs
from imbue.mngr_kanpan.tui import _build_field_color_palette
from imbue.mngr_kanpan.tui import _build_mark_palette
from imbue.mngr_kanpan.tui import _carry_forward_fields
from imbue.mngr_kanpan.tui import _clear_focus
from imbue.mngr_kanpan.tui import _compute_board_column_widths
from imbue.mngr_kanpan.tui import _dispatch_command
from imbue.mngr_kanpan.tui import _execute_marks
from imbue.mngr_kanpan.tui import _execute_next_in_batch
from imbue.mngr_kanpan.tui import _field_cell_markup
from imbue.mngr_kanpan.tui import _field_cell_text
from imbue.mngr_kanpan.tui import _finish_batch_execution
from imbue.mngr_kanpan.tui import _flatten_markup_to_muted
from imbue.mngr_kanpan.tui import _format_section_heading
from imbue.mngr_kanpan.tui import _get_focused_entry
from imbue.mngr_kanpan.tui import _get_name_cell_markup
from imbue.mngr_kanpan.tui import _get_state_attr
from imbue.mngr_kanpan.tui import _is_focus_on_first_selectable
from imbue.mngr_kanpan.tui import _load_user_commands
from imbue.mngr_kanpan.tui import _on_batch_item_poll
from imbue.mngr_kanpan.tui import _prune_orphaned_marks
from imbue.mngr_kanpan.tui import _refresh_display
from imbue.mngr_kanpan.tui import _resolve_section_order
from imbue.mngr_kanpan.tui import _restore_footer
from imbue.mngr_kanpan.tui import _run_shell_command
from imbue.mngr_kanpan.tui import _show_transient_message
from imbue.mngr_kanpan.tui import _submit_batch_item
from imbue.mngr_kanpan.tui import _toggle_mark
from imbue.mngr_kanpan.tui import _unmark_all
from imbue.mngr_kanpan.tui import _unmark_focused
from imbue.mngr_kanpan.tui import _update_mark_count_footer
from imbue.mngr_kanpan.tui import _update_row_mark
from imbue.mngr_kanpan.tui import _update_snapshot_mute

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


def _make_pr(
    number: int = 42,
    state: PrState = PrState.OPEN,
    is_draft: bool = False,
) -> PrField:
    return PrField(
        number=number,
        title="Test PR",
        state=state,
        url=f"https://github.com/owner/repo/pull/{number}",
        head_branch="mngr/test-agent",
        is_draft=is_draft,
    )


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


def _make_snapshot(
    entries: tuple[AgentBoardEntry, ...] = (),
    errors: tuple[str, ...] = (),
) -> BoardSnapshot:
    return BoardSnapshot(entries=entries, errors=errors, fetch_time_seconds=1.5)


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


def test_get_state_attr_running() -> None:
    entry = _make_entry(state=AgentLifecycleState.RUNNING)
    assert _get_state_attr(entry) == "state_running"


def test_get_state_attr_waiting() -> None:
    entry = _make_entry(state=AgentLifecycleState.WAITING)
    assert _get_state_attr(entry) == "state_attention"


def test_get_state_attr_done() -> None:
    entry = _make_entry(state=AgentLifecycleState.DONE)
    assert _get_state_attr(entry) == ""


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
    snapshot = _make_snapshot(entries=())
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert idx_map == {}


def test_build_board_widgets_one_entry() -> None:
    entry = _make_entry(section=BoardSection.STILL_COOKING)
    snapshot = _make_snapshot(entries=(entry,))
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert len(idx_map) == 1


def test_build_board_widgets_errors_displayed() -> None:
    snapshot = _make_snapshot(entries=(), errors=("Error 1",))
    walker, _ = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    texts = [w.text if hasattr(w, "text") else "" for w in walker]
    found_error = any("Error 1" in str(t) for t in texts)
    assert found_error


def test_build_board_widgets_groups_by_section() -> None:
    e1 = _make_entry(name="a", section=BoardSection.STILL_COOKING)
    e2 = _make_entry(name="b", section=BoardSection.PR_MERGED)
    snapshot = _make_snapshot(entries=(e1, e2))
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert len(idx_map) == 2


# =============================================================================
# Column assembly
# =============================================================================


def test_assemble_column_defs_no_order_no_custom() -> None:
    result = _assemble_column_defs(_BUILTIN_COLUMN_DEFS, [], None)
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


# =============================================================================
# Mark palette
# =============================================================================


def test_build_mark_palette_no_markable() -> None:
    commands = {"r": CustomCommand(name="refresh")}
    entries, names = _build_mark_palette(commands)
    assert entries == []
    assert names == ()


def test_build_mark_palette_markable() -> None:
    commands = {"d": CustomCommand(name="delete", markable="light red")}
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


def test_restore_footer() -> None:
    state = _make_state()
    state.steady_footer_text = "  Steady"
    _restore_footer(state)
    assert state.footer_left_text.text == "  Steady"


def test_update_snapshot_mute() -> None:
    entry = _make_entry(is_muted=False)
    state = _make_state(snapshot=_make_snapshot(entries=(entry,)))
    _update_snapshot_mute(state, AgentName("test-agent"), True)
    assert state.snapshot is not None
    assert state.snapshot.entries[0].is_muted is True


def test_prune_orphaned_marks() -> None:
    entry = _make_entry(name="agent-a")
    state = _make_state(snapshot=_make_snapshot(entries=(entry,)))
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
    entry = _make_entry(cells={"ci": CellDisplay(text="failing", color="light red")})
    assert _field_cell_text(entry, "ci") == "failing"


def test_field_cell_text_absent() -> None:
    entry = _make_entry()
    assert _field_cell_text(entry, "ci") == ""


def test_field_cell_markup_with_color() -> None:
    entry = _make_entry(cells={"ci": CellDisplay(text="failing", color="light red")})
    markup = _field_cell_markup(entry, "ci")
    assert isinstance(markup, tuple)
    assert markup[1] == "failing"


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
        return {"mock_field": "MOCK", "empty_header": ""}

    @property
    def field_types(self) -> dict[str, type[FieldValue]]:
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
    # All columns returned, including empty-header ones (filtered later by _assemble_column_defs)
    assert "mock_field" in names
    assert "empty_header" in names


def test_build_data_source_column_defs_deduplicates() -> None:
    defs = _build_data_source_column_defs([_MockDataSource(), _MockDataSource()])
    names = [d.name for d in defs]
    assert names.count("mock_field") == 1


# =============================================================================
# Field color palette
# =============================================================================


def test_build_field_color_palette_none_snapshot() -> None:
    entries, names = _build_field_color_palette(None)
    assert entries == []
    assert names == ()


def test_build_field_color_palette_with_colors() -> None:
    entry = _make_entry(cells={"ci": CellDisplay(text="failing", color="light red")})
    snapshot = _make_snapshot(entries=(entry,))
    entries, names = _build_field_color_palette(snapshot)
    assert len(entries) == 2
    assert "field_ci_light red" in names


def test_build_field_color_palette_no_colors() -> None:
    entry = _make_entry(cells={"pr": CellDisplay(text="#42")})
    snapshot = _make_snapshot(entries=(entry,))
    entries, names = _build_field_color_palette(snapshot)
    assert entries == []


# =============================================================================
# Flatten markup
# =============================================================================


def test_flatten_markup_to_muted_string() -> None:
    result = _flatten_markup_to_muted("hello")
    assert result == ("muted", "hello")


def test_flatten_markup_to_muted_tuple() -> None:
    result = _flatten_markup_to_muted(("some_attr", "text"))
    assert result == ("muted", "text")


def test_flatten_markup_to_muted_list() -> None:
    result = _flatten_markup_to_muted([("attr", "a"), "b"])
    assert result == ("muted", "ab")


# =============================================================================
# Carry forward fields
# =============================================================================


def test_carry_forward_fields_merges() -> None:
    old_entry = _make_entry(
        name="a",
        fields={"pr": _make_pr(), "commits_ahead": CommitsAheadField(count=3, has_work_dir=True)},
        cells={"pr": _make_pr().display(), "commits_ahead": CommitsAheadField(count=3, has_work_dir=True).display()},
    )
    new_entry = _make_entry(
        name="a",
        fields={"commits_ahead": CommitsAheadField(count=5, has_work_dir=True)},
        cells={"commits_ahead": CommitsAheadField(count=5, has_work_dir=True).display()},
    )
    old_snapshot = _make_snapshot(entries=(old_entry,))
    new_snapshot = _make_snapshot(entries=(new_entry,))
    result = _carry_forward_fields(old_snapshot, new_snapshot)
    merged = result.entries[0]
    assert "pr" in merged.fields
    assert "commits_ahead" in merged.fields
    ca_field = merged.fields["commits_ahead"]
    assert isinstance(ca_field, CommitsAheadField)
    assert ca_field.count == 5


def test_carry_forward_fields_new_agent() -> None:
    new_entry = _make_entry(name="new-agent")
    old_snapshot = _make_snapshot(entries=())
    new_snapshot = _make_snapshot(entries=(new_entry,))
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


def test_field_cell_markup_ci_failing_uses_color_attr() -> None:
    """CI FAILING cell has color='light red', so markup uses field_ci_light red attr."""
    ci = CiField(status=CiStatus.FAILING)
    cell = ci.display()
    entry = _make_entry(
        fields={FIELD_CI: ci},
        cells={FIELD_CI: cell},
    )
    markup = _field_cell_markup(entry, FIELD_CI)
    assert isinstance(markup, tuple)
    assert markup[0] == f"field_{FIELD_CI}_light red"
    assert markup[1] == cell.text


def test_field_cell_markup_ci_pending_uses_color_attr() -> None:
    """CI PENDING cell has color='yellow', so markup uses field_ci_yellow attr."""
    ci = CiField(status=CiStatus.PENDING)
    cell = ci.display()
    entry = _make_entry(
        fields={FIELD_CI: ci},
        cells={FIELD_CI: cell},
    )
    markup = _field_cell_markup(entry, FIELD_CI)
    assert isinstance(markup, tuple)
    assert markup[0] == f"field_{FIELD_CI}_yellow"
    assert markup[1] == cell.text


def test_field_cell_markup_ci_passing_uses_color_attr() -> None:
    """CI PASSING cell has color='light green', so markup uses field_ci_light green attr."""
    ci = CiField(status=CiStatus.PASSING)
    cell = ci.display()
    entry = _make_entry(
        fields={FIELD_CI: ci},
        cells={FIELD_CI: cell},
    )
    markup = _field_cell_markup(entry, FIELD_CI)
    assert isinstance(markup, tuple)
    assert markup[0] == f"field_{FIELD_CI}_light green"
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
    snapshot = _make_snapshot(entries=(entry,))
    marks = {AgentName("agent-a"): "d"}
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS, marks=marks)
    assert len(idx_map) == 1


def test_build_board_widgets_muted_entry() -> None:
    entry = _make_entry(name="muted-agent", is_muted=True, section=BoardSection.MUTED)
    snapshot = _make_snapshot(entries=(entry,))
    walker, idx_map = _build_board_widgets(snapshot, _BUILTIN_COLUMN_DEFS)
    assert len(idx_map) == 1


def test_build_board_widgets_multiple_sections() -> None:
    e1 = _make_entry(name="a", section=BoardSection.STILL_COOKING)
    e2 = _make_entry(name="b", section=BoardSection.PR_BEING_REVIEWED)
    e3 = _make_entry(name="c", section=BoardSection.PRS_FAILED)
    snapshot = _make_snapshot(entries=(e1, e2, e3))
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
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    snapshot = _make_snapshot(entries=(entry,))
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
    state = _make_state(snapshot=_make_snapshot(entries=entries), commands=commands)
    state.mark_attr_names = ("mark_d", "mark_p")
    walker, idx_map = _build_board_widgets(_make_snapshot(entries=entries), _BUILTIN_COLUMN_DEFS)
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


def test_toggle_mark_removes_existing_mark() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
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
    _unmark_focused(state)


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
    state.snapshot = _make_snapshot(entries=())
    _prune_orphaned_marks(state)
    assert AgentName("gone-agent") not in state.marks


# =============================================================================
# _dispatch_command
# =============================================================================


def test_dispatch_command_markable_key_toggles_mark() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    commands = {"d": CustomCommand(name="delete", markable="light red")}
    state = _make_state_with_walker((entry,))
    state.commands = commands
    state.mark_attr_names = ("mark_d",)
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    _dispatch_command(state, "d", commands["d"])
    assert AgentName("agent-a") in state.marks


def test_dispatch_command_unmark_key_removes_mark() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    state.marks[AgentName("agent-a")] = "d"
    agent_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(agent_idx)
    unmark_cmd = CustomCommand(name="unmark")
    state.commands = {_BUILTIN_COMMAND_KEY_UNMARK: unmark_cmd}
    _dispatch_command(state, _BUILTIN_COMMAND_KEY_UNMARK, unmark_cmd)
    assert AgentName("agent-a") not in state.marks


def test_dispatch_command_execute_key_with_marks() -> None:
    # Register a real "d" command so _start_batch_execution has work to submit.
    # With loop=None, the future is submitted but never polled, so executing stays True.
    mark_cmd = CustomCommand(name="do-thing", command="echo hi")
    state = _make_state(commands={"d": mark_cmd})
    state.marks = {AgentName("a"): "d"}
    execute_cmd = CustomCommand(name="execute")
    _dispatch_command(state, _BUILTIN_COMMAND_KEY_EXECUTE, execute_cmd)
    # Should start batch execution (sets executing=True)
    assert state.executing is True
    # Clean up the executor to avoid resource warnings
    if state.executor is not None:
        state.executor.shutdown(wait=False)


# =============================================================================
# _refresh_display
# =============================================================================


def test_refresh_display_updates_walker() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    snapshot = _make_snapshot(entries=(entry,))
    state = _make_state(snapshot=snapshot)
    _refresh_display(state)
    assert state.list_walker is not None
    assert len(state.index_to_entry) == 1


def test_refresh_display_restores_focus() -> None:
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    snapshot = _make_snapshot(entries=(entry,))
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


# =============================================================================
# _load_user_commands and _build_command_map
# =============================================================================


def test_load_user_commands_from_custom_command_instance() -> None:
    cmd = CustomCommand(name="my-cmd", command="echo hi")
    config = KanpanPluginConfig(commands={"c": cmd})
    ctx = cast(MngrContext, SimpleNamespace(get_plugin_config=lambda name, cls: config))
    result = _load_user_commands(ctx)
    assert "c" in result
    assert result["c"].name == "my-cmd"


def test_load_user_commands_from_dict() -> None:
    config = KanpanPluginConfig(commands={"c": CustomCommand(name="my-cmd", command="echo hi")})
    ctx = cast(MngrContext, SimpleNamespace(get_plugin_config=lambda name, cls: config))
    result = _load_user_commands(ctx)
    assert "c" in result


def test_build_command_map_includes_builtins() -> None:
    config = KanpanPluginConfig()
    ctx = cast(MngrContext, SimpleNamespace(get_plugin_config=lambda name, cls: config))
    result = _build_command_map(ctx)
    # "r" is the builtin refresh key; "q" is quit and not a mapped command
    assert "r" in result
    assert "q" not in result


def test_build_command_map_user_overrides_builtin() -> None:
    custom = CustomCommand(name="my-refresh", command="echo refresh")
    config = KanpanPluginConfig(commands={_BUILTIN_COMMAND_KEY_REFRESH: custom})
    ctx = cast(MngrContext, SimpleNamespace(get_plugin_config=lambda name, cls: config))
    result = _build_command_map(ctx)
    assert result[_BUILTIN_COMMAND_KEY_REFRESH].name == "my-refresh"


def test_build_command_map_excludes_disabled() -> None:
    disabled = CustomCommand(name="disabled-cmd", enabled=False)
    config = KanpanPluginConfig(commands={"z": disabled})
    ctx = cast(MngrContext, SimpleNamespace(get_plugin_config=lambda name, cls: config))
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
    snapshot = _make_snapshot(entries=(entry,))
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
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    commands = {
        _BUILTIN_COMMAND_KEY_PUSH: CustomCommand(name="mark push", markable="yellow"),
    }
    state = _make_state(snapshot=_make_snapshot(entries=(entry,)), commands=commands)
    state.mark_attr_names = ("mark_p",)
    walker, idx_map = _build_board_widgets(_make_snapshot(entries=(entry,)), _BUILTIN_COLUMN_DEFS)
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
    _finish_batch_execution(state, ["op1: ok", "op2: ok"])
    assert state.executing is False
    assert "2" in state.footer_left_text.text


def test_finish_batch_execution_with_failures() -> None:
    state = _make_state()
    state.executing = True
    _finish_batch_execution(state, ["op1: ok", "op2: failed (err)"])
    assert state.executing is False
    assert "failed" in state.footer_left_text.text or "1 failed" in state.footer_left_text.text


def test_finish_batch_execution_empty_results() -> None:
    state = _make_state()
    state.executing = True
    _finish_batch_execution(state, [])
    assert state.executing is False


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
    results: list[str] = []
    _on_batch_item_poll(mock_loop, (state, future, [item], results, 0, item))
    assert any("failed" in r for r in results)


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
    results: list[str] = []
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
        cmd=CustomCommand(name="push"),
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
        cmd=CustomCommand(name="push"),
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
    entry = _make_entry(name="agent-a", section=BoardSection.STILL_COOKING)
    state = _make_state_with_walker((entry,))
    a_idx = next(k for k, v in state.index_to_entry.items() if v.name == AgentName("agent-a"))
    state.list_walker.set_focus(a_idx)
    cmd = CustomCommand(name="say-hi", command="true")
    _run_shell_command(state, cmd)
    assert state.executor is not None
    state.executor.shutdown(wait=True)


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
    results: list[str] = []
    _execute_next_in_batch(state, [item], results, 0)
    assert any("skipped" in r for r in results)
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
    walker, _ = _build_board_widgets(_make_snapshot(entries=entries), _BUILTIN_COLUMN_DEFS)
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
        _make_snapshot(entries=entries),
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
        _make_snapshot(entries=entries),
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
