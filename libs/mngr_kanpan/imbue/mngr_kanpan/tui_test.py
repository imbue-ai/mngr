"""Unit tests for the kanpan TUI."""

from types import SimpleNamespace
from typing import Any

import pytest
from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.widget.attr_map import AttrMap
from urwid.widget.filler import Filler
from urwid.widget.frame import Frame
from urwid.widget.text import Text

from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import CommitsAheadField
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_source import PrField
from imbue.mngr_kanpan.data_source import PrState
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import CustomCommand
from imbue.mngr_kanpan.tui import _BUILTIN_COLUMN_DEFS
from imbue.mngr_kanpan.tui import _BatchWorkItem
from imbue.mngr_kanpan.tui import _KanpanInputHandler
from imbue.mngr_kanpan.tui import _KanpanState
from imbue.mngr_kanpan.tui import _assemble_column_defs
from imbue.mngr_kanpan.tui import _batch_item_label
from imbue.mngr_kanpan.tui import _build_board_widgets
from imbue.mngr_kanpan.tui import _build_data_source_column_defs
from imbue.mngr_kanpan.tui import _build_field_color_palette
from imbue.mngr_kanpan.tui import _build_mark_palette
from imbue.mngr_kanpan.tui import _carry_forward_fields
from imbue.mngr_kanpan.tui import _clear_focus
from imbue.mngr_kanpan.tui import _field_cell_markup
from imbue.mngr_kanpan.tui import _field_cell_text
from imbue.mngr_kanpan.tui import _field_cell_url
from imbue.mngr_kanpan.tui import _flatten_markup_to_muted
from imbue.mngr_kanpan.tui import _format_section_heading
from imbue.mngr_kanpan.tui import _get_name_cell_markup
from imbue.mngr_kanpan.tui import _get_state_attr
from imbue.mngr_kanpan.tui import _prune_orphaned_marks
from imbue.mngr_kanpan.tui import _restore_footer
from imbue.mngr_kanpan.tui import _show_transient_message
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


def test_field_cell_url_present() -> None:
    entry = _make_entry(cells={"pr": CellDisplay(text="#42", url="https://github.com/pull/42")})
    assert _field_cell_url(entry, "pr") == "https://github.com/pull/42"


def test_field_cell_url_absent() -> None:
    entry = _make_entry()
    assert _field_cell_url(entry, "pr") == ""


def test_field_cell_url_no_url() -> None:
    entry = _make_entry(cells={"ci": CellDisplay(text="passing")})
    assert _field_cell_url(entry, "ci") == ""


# =============================================================================
# Data source column defs
# =============================================================================


class _MockDataSource:
    @property
    def name(self) -> str:
        return "mock"

    @property
    def columns(self) -> dict[str, str]:
        return {"mock_field": "MOCK", "empty_header": ""}

    @property
    def field_types(self) -> dict[str, type[FieldValue]]:
        return {}

    def compute(
        self,
        agents: Any,
        cached_fields: Any,
        mngr_ctx: Any,
    ) -> tuple[dict[Any, dict[str, FieldValue]], list[str]]:
        return {}, []


def test_build_data_source_column_defs() -> None:
    defs = _build_data_source_column_defs([_MockDataSource()])
    names = [d.name for d in defs]
    assert "mock_field" in names
    assert "empty_header" not in names


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
