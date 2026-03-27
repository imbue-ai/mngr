"""Unit tests for the kanpan TUI."""

import subprocess
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.event_loop.main_loop import MainLoop
from urwid.widget.attr_map import AttrMap
from urwid.widget.columns import Columns
from urwid.widget.text import Text

from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import CheckStatus
from imbue.mngr_kanpan.data_types import ColumnData
from imbue.mngr_kanpan.data_types import CustomColumnConfig
from imbue.mngr_kanpan.data_types import CustomCommand
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.data_types import PrInfo
from imbue.mngr_kanpan.data_types import PrState
from imbue.mngr_kanpan.data_types import RefreshHook
from imbue.mngr_kanpan.testing import make_pr_info
from imbue.mngr_kanpan.tui import DEFAULT_REFRESH_INTERVAL_SECONDS
from imbue.mngr_kanpan.tui import _BOARD_COLUMN_DEFS
from imbue.mngr_kanpan.tui import _KanpanInputHandler
from imbue.mngr_kanpan.tui import _KanpanState
from imbue.mngr_kanpan.tui import _assemble_column_defs
from imbue.mngr_kanpan.tui import _build_board_widgets
from imbue.mngr_kanpan.tui import _build_column_palette
from imbue.mngr_kanpan.tui import _build_command_map
from imbue.mngr_kanpan.tui import _build_custom_column_defs
from imbue.mngr_kanpan.tui import _build_mark_palette
from imbue.mngr_kanpan.tui import _carry_forward_pr_data
from imbue.mngr_kanpan.tui import _classify_entry
from imbue.mngr_kanpan.tui import _clear_focus
from imbue.mngr_kanpan.tui import _custom_col_markup
from imbue.mngr_kanpan.tui import _custom_col_text
from imbue.mngr_kanpan.tui import _dispatch_command
from imbue.mngr_kanpan.tui import _finish_refresh
from imbue.mngr_kanpan.tui import _format_section_heading
from imbue.mngr_kanpan.tui import _get_focused_entry
from imbue.mngr_kanpan.tui import _get_name_cell_markup
from imbue.mngr_kanpan.tui import _get_state_attr
from imbue.mngr_kanpan.tui import _is_focus_on_first_selectable
from imbue.mngr_kanpan.tui import _load_refresh_hooks
from imbue.mngr_kanpan.tui import _load_user_columns
from imbue.mngr_kanpan.tui import _load_user_commands
from imbue.mngr_kanpan.tui import _mute_focused_agent
from imbue.mngr_kanpan.tui import _on_auto_refresh_alarm
from imbue.mngr_kanpan.tui import _on_custom_command_poll
from imbue.mngr_kanpan.tui import _on_mute_persist_poll
from imbue.mngr_kanpan.tui import _on_restore_footer
from imbue.mngr_kanpan.tui import _on_spinner_tick
from imbue.mngr_kanpan.tui import _refresh_display
from imbue.mngr_kanpan.tui import _request_refresh
from imbue.mngr_kanpan.tui import _restore_footer
from imbue.mngr_kanpan.tui import _run_shell_command
from imbue.mngr_kanpan.tui import _schedule_next_refresh
from imbue.mngr_kanpan.tui import _show_transient_message
from imbue.mngr_kanpan.tui import _start_local_refresh
from imbue.mngr_kanpan.tui import _toggle_mark
from imbue.mngr_kanpan.tui import _unmark_all
from imbue.mngr_kanpan.tui import _unmark_focused
from imbue.mngr_kanpan.tui import _update_snapshot_mute

# =============================================================================
# Helpers
# =============================================================================


class _CallTracker:
    """Lightweight call tracker to replace MagicMock.assert_called patterns."""

    def __init__(self) -> None:
        self.call_count: int = 0

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.call_count += 1


def _make_mock_loop() -> Any:
    """Create a lightweight loop substitute with a trackable set_alarm_in."""
    tracker = _CallTracker()
    return SimpleNamespace(set_alarm_in=tracker, _alarm_tracker=tracker)


def _make_entry(
    name: str = "test-agent",
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    pr: PrInfo | None = None,
    pr_state: PrState | None = None,
    work_dir: Path | None = None,
    commits_ahead: int | None = None,
    create_pr_url: str | None = None,
    is_muted: bool = False,
    labels: dict[str, str] | None = None,
    plugin_data: dict[str, Any] | None = None,
) -> AgentBoardEntry:
    if pr is None and pr_state is not None:
        pr = _make_pr(state=pr_state)
    return AgentBoardEntry(
        name=AgentName(name),
        state=state,
        provider_name=ProviderInstanceName("local"),
        work_dir=work_dir,
        pr=pr,
        commits_ahead=commits_ahead,
        create_pr_url=create_pr_url,
        is_muted=is_muted,
        column_data=ColumnData(
            labels=labels or {},
            plugin_data=plugin_data or {},
        ),
    )


def _make_pr(
    number: int = 42,
    state: PrState = PrState.OPEN,
    check_status: CheckStatus = CheckStatus.PASSING,
) -> PrInfo:
    return PrInfo(
        number=number,
        title="Test PR",
        state=state,
        url="https://github.com/owner/repo/pull/42",
        head_branch="mngr/test-agent",
        check_status=check_status,
        is_draft=False,
    )


def _make_snapshot(
    entries: tuple[AgentBoardEntry, ...] = (),
    errors: tuple[str, ...] = (),
) -> BoardSnapshot:
    return BoardSnapshot(entries=entries, errors=errors, repo_pr_loaded={}, fetch_time_seconds=1.5)


def _make_state(
    snapshot: BoardSnapshot | None = None,
    commands: dict[str, CustomCommand] | None = None,
) -> _KanpanState:
    footer_left_text = Text("  Loading...")
    footer_left_attr = AttrMap(footer_left_text, "footer")
    footer_right = Text("r: refresh  q: quit")
    frame = SimpleNamespace(body=None)
    mngr_ctx = SimpleNamespace(config=SimpleNamespace(plugins={}))
    return _KanpanState.model_construct(
        mngr_ctx=mngr_ctx,
        snapshot=snapshot,
        frame=frame,
        footer_left_text=footer_left_text,
        footer_left_attr=footer_left_attr,
        footer_right=footer_right,
        commands=commands or {},
        spinner_index=0,
        refresh_future=None,
        executor=None,
        marks={},
        executing=False,
        execute_status="",
        index_to_entry={},
        list_walker=None,
        focused_agent_name=None,
        steady_footer_text="  Loading...",
        mark_attr_names=(),
    )


def _make_state_with_focus(
    entries: tuple[AgentBoardEntry, ...],
    commands: dict[str, CustomCommand] | None = None,
) -> _KanpanState:
    """Create a state with board widgets built and focus on the first agent."""
    snapshot = _make_snapshot(entries=entries)
    state = _make_state(snapshot=snapshot, commands=commands)
    walker, index_to_entry = _build_board_widgets(snapshot, _BOARD_COLUMN_DEFS)
    state.list_walker = walker
    state.index_to_entry = index_to_entry
    agent_idx = next(iter(index_to_entry.keys()))
    walker.set_focus(agent_idx)
    return state


def _make_done_future(result: subprocess.CompletedProcess[str]) -> Future[subprocess.CompletedProcess[str]]:
    future: Future[subprocess.CompletedProcess[str]] = Future()
    future.set_result(result)
    return future


def _make_failed_future(error: Exception) -> Future[subprocess.CompletedProcess[str]]:
    future: Future[subprocess.CompletedProcess[str]] = Future()
    future.set_exception(error)
    return future


def _text_from_widget(widget: Text) -> str:
    """Extract plain text content from a single Text widget."""
    raw = widget.text
    if isinstance(raw, str):
        return raw
    parts: list[str] = []
    for seg in raw:
        if isinstance(seg, tuple):
            parts.append(str(seg[1]))
        else:
            parts.append(str(seg))
    return "".join(parts)


def _extract_text(walker: list[object]) -> list[str]:
    """Extract plain text from all Text and Columns widgets in a walker."""
    texts: list[str] = []
    for widget in walker:
        inner = widget.original_widget if isinstance(widget, AttrMap) else widget
        if isinstance(inner, Text):
            texts.append(_text_from_widget(inner))
        elif isinstance(inner, Columns):
            cell_texts = [_text_from_widget(child) for child, _options in inner.contents if isinstance(child, Text)]
            texts.append(" ".join(cell_texts))
    return texts


def _text_contains(texts: list[str], substring: str) -> bool:
    return any(substring in t for t in texts)


# =============================================================================
# Tests for _classify_entry
# =============================================================================


def test_classify_entry_muted_always_goes_to_muted_section() -> None:
    entry = _make_entry(is_muted=True, pr=_make_pr(state=PrState.MERGED))
    assert _classify_entry(entry, {}) == BoardSection.MUTED


def test_classify_entry_no_pr_is_still_cooking() -> None:
    assert _classify_entry(_make_entry(pr=None), {}) == BoardSection.STILL_COOKING


def test_classify_entry_merged_pr() -> None:
    assert _classify_entry(_make_entry(pr=_make_pr(state=PrState.MERGED)), {}) == BoardSection.PR_MERGED


def test_classify_entry_closed_pr() -> None:
    assert _classify_entry(_make_entry(pr=_make_pr(state=PrState.CLOSED)), {}) == BoardSection.PR_CLOSED


def test_classify_entry_open_pr() -> None:
    assert _classify_entry(_make_entry(pr=_make_pr(state=PrState.OPEN)), {}) == BoardSection.PR_BEING_REVIEWED


def test_classify_entry_failed_repo_goes_to_prs_failed() -> None:
    entry = _make_entry(pr=None, labels={"remote": "git@github.com:org/repo.git"})
    assert _classify_entry(entry, {"org/repo": False}) == BoardSection.PRS_FAILED


def test_classify_entry_loaded_repo_stays_in_still_cooking() -> None:
    entry = _make_entry(pr=None, labels={"remote": "git@github.com:org/repo.git"})
    assert _classify_entry(entry, {"org/repo": True}) == BoardSection.STILL_COOKING


def test_classify_entry_no_remote_label_stays_in_still_cooking() -> None:
    """Agents without a remote label are not considered failed -- they have no upstream."""
    entry = _make_entry(pr=None)
    assert _classify_entry(entry, {}) == BoardSection.STILL_COOKING


def test_classify_entry_repo_not_in_loaded_dict_stays_in_still_cooking() -> None:
    """Agents whose repo is not tracked in repo_pr_loaded are not PRS_FAILED."""
    entry = _make_entry(pr=None, labels={"remote": "git@github.com:org/repo.git"})
    assert _classify_entry(entry, {}) == BoardSection.STILL_COOKING


# =============================================================================
# Tests for _get_state_attr
# =============================================================================


def test_get_state_attr_running_gets_green() -> None:
    assert _get_state_attr(_make_entry(state=AgentLifecycleState.RUNNING)) == "state_running"


def test_get_state_attr_waiting_gets_attention() -> None:
    assert _get_state_attr(_make_entry(state=AgentLifecycleState.WAITING)) == "state_attention"


def test_get_state_attr_stopped_gets_no_color() -> None:
    assert _get_state_attr(_make_entry(state=AgentLifecycleState.STOPPED)) == ""


# =============================================================================
# Tests for _format_section_heading
# =============================================================================


def test_format_section_heading_merged() -> None:
    markup = _format_section_heading(BoardSection.PR_MERGED, 5)
    assert markup[0] == ("section_done", "Done")
    assert "PR merged" in markup[1]
    assert "(5)" in markup[1]


def test_format_section_heading_muted_has_no_suffix() -> None:
    markup = _format_section_heading(BoardSection.MUTED, 2)
    assert markup[0] == ("section_muted", "Muted")
    assert "(2)" in markup[1]


def test_format_section_heading_still_cooking() -> None:
    markup = _format_section_heading(BoardSection.STILL_COOKING, 1)
    assert markup[0] == ("section_in_progress", "In progress")
    assert "no PR yet" in markup[1]


# =============================================================================
# Tests for _build_board_widgets
# =============================================================================


def test_build_board_widgets_none_snapshot_shows_loading() -> None:
    walker, _ = _build_board_widgets(None, _BOARD_COLUMN_DEFS)
    assert len(walker) == 1
    assert isinstance(walker[0], Text)


def test_build_board_widgets_empty_snapshot_shows_no_agents() -> None:
    walker, _ = _build_board_widgets(_make_snapshot(), _BOARD_COLUMN_DEFS)
    assert len(walker) == 1
    widget = walker[0]
    assert isinstance(widget, Text)
    assert "No agents found" in str(widget.get_text()[0])


def test_build_board_widgets_with_entries_creates_sections() -> None:
    entries = (_make_entry(name="cooking"), _make_entry(name="merged", pr=_make_pr(state=PrState.MERGED)))
    walker, index_to_entry = _build_board_widgets(_make_snapshot(entries=entries), _BOARD_COLUMN_DEFS)
    assert len(walker) >= 4
    assert len(index_to_entry) == 2


def test_build_board_widgets_populates_index_to_entry() -> None:
    entries = (_make_entry(name="agent-a"), _make_entry(name="agent-b"))
    _, index_to_entry = _build_board_widgets(_make_snapshot(entries=entries), _BOARD_COLUMN_DEFS)
    names = {entry.name for entry in index_to_entry.values()}
    assert AgentName("agent-a") in names
    assert AgentName("agent-b") in names


def test_build_board_widgets_with_errors_shows_them() -> None:
    walker, _ = _build_board_widgets(_make_snapshot(errors=("Something went wrong",)), _BOARD_COLUMN_DEFS)
    all_text = " ".join(str(w.get_text()[0]) for w in walker if isinstance(w, Text))
    assert "Errors:" in all_text
    assert "Something went wrong" in all_text


# =============================================================================
# Tests for _get_focused_entry, _is_focus_on_first_selectable, _clear_focus
# =============================================================================


def test_get_focused_entry_returns_none_when_no_walker() -> None:
    assert _get_focused_entry(_make_state()) is None


def test_get_focused_entry_returns_entry_when_focused() -> None:
    state = _make_state_with_focus(entries=(_make_entry(name="focused-agent"),))
    entry = _get_focused_entry(state)
    assert entry is not None
    assert entry.name == AgentName("focused-agent")


def test_is_focus_on_first_selectable_true() -> None:
    assert _is_focus_on_first_selectable(_make_state_with_focus(entries=(_make_entry(),))) is True


def test_is_focus_on_first_selectable_false_when_no_walker() -> None:
    assert _is_focus_on_first_selectable(_make_state()) is False


def test_clear_focus_moves_to_top() -> None:
    state = _make_state_with_focus(entries=(_make_entry(name="agent-a"), _make_entry(name="agent-b")))
    _clear_focus(state)
    assert state.focused_agent_name is None
    _, focus_pos = state.list_walker.get_focus()
    assert focus_pos == 0


# =============================================================================
# Tests for _update_snapshot_mute
# =============================================================================


def test_update_snapshot_mute_sets_muted() -> None:
    entries = (_make_entry(name="agent-a", is_muted=False), _make_entry(name="agent-b", is_muted=False))
    state = _make_state(snapshot=_make_snapshot(entries=entries))
    _update_snapshot_mute(state, AgentName("agent-a"), True)
    assert state.snapshot is not None
    updated = {e.name: e for e in state.snapshot.entries}
    assert updated[AgentName("agent-a")].is_muted is True
    assert updated[AgentName("agent-b")].is_muted is False


def test_update_snapshot_mute_unsets_muted() -> None:
    state = _make_state(snapshot=_make_snapshot(entries=(_make_entry(name="agent-a", is_muted=True),)))
    _update_snapshot_mute(state, AgentName("agent-a"), False)
    assert state.snapshot is not None
    assert state.snapshot.entries[0].is_muted is False


def test_update_snapshot_mute_no_snapshot_does_not_create_one() -> None:
    state = _make_state(snapshot=None)
    _update_snapshot_mute(state, AgentName("agent-a"), True)
    assert state.snapshot is None


# =============================================================================
# Tests for _KanpanInputHandler
# =============================================================================


def test_input_handler_q_exits() -> None:
    with pytest.raises(ExitMainLoop):
        _KanpanInputHandler(state=_make_state())("q")


def test_input_handler_ctrl_c_exits() -> None:
    with pytest.raises(ExitMainLoop):
        _KanpanInputHandler(state=_make_state())("ctrl c")


def test_input_handler_ignores_mouse_events() -> None:
    state = _make_state()
    result = _KanpanInputHandler(state=state)(("mouse press", 1, 0, 0))
    assert result is None


def test_input_handler_passes_through_navigation_keys() -> None:
    handler = _KanpanInputHandler(state=_make_state())
    for key in ("down", "page up", "page down", "home", "end"):
        assert handler(key) is None


def test_input_handler_swallows_unknown_keys() -> None:
    assert _KanpanInputHandler(state=_make_state())("z") is True


def test_input_handler_up_on_first_selectable_clears_focus() -> None:
    state = _make_state_with_focus(entries=(_make_entry(name="agent-a"),))
    result = _KanpanInputHandler(state=state)("up")
    assert result is True
    assert state.focused_agent_name is None


# =============================================================================
# Tests for _build_command_map and _load_user_commands
# =============================================================================


def _make_mngr_ctx_with_plugins(
    plugins: dict[PluginName, object] | None = None,
) -> Any:
    """Create a SimpleNamespace that mimics MngrContext.get_plugin_config behavior."""
    plugin_dict = plugins or {}

    def get_plugin_config(name: str, config_type: type) -> object:
        config = plugin_dict.get(PluginName(name))
        if config is None:
            return config_type()
        return config

    return SimpleNamespace(
        config=SimpleNamespace(plugins=plugin_dict),
        get_plugin_config=get_plugin_config,
    )


def test_build_command_map_returns_builtins_with_no_user_config() -> None:
    commands = _build_command_map(_make_mngr_ctx_with_plugins())
    assert {"r", "p", "d", "m"} <= set(commands.keys())


def test_build_command_map_user_command_overrides_builtin() -> None:
    mngr_ctx = _make_mngr_ctx_with_plugins(
        {
            PluginName("kanpan"): KanpanPluginConfig(
                commands={"r": CustomCommand(name="custom-refresh", command="echo refresh")}
            )
        }
    )
    commands = _build_command_map(mngr_ctx)
    assert commands["r"].name == "custom-refresh"


def test_build_command_map_disabled_command_is_excluded() -> None:
    mngr_ctx = _make_mngr_ctx_with_plugins(
        {PluginName("kanpan"): KanpanPluginConfig(commands={"d": CustomCommand(name="delete", enabled=False)})}
    )
    assert "d" not in _build_command_map(mngr_ctx)


def test_load_user_commands_no_kanpan_plugin_returns_empty() -> None:
    assert _load_user_commands(_make_mngr_ctx_with_plugins()) == {}


def test_load_user_commands_handles_dict_values() -> None:
    config = KanpanPluginConfig.model_construct(
        enabled=True, commands={"x": {"name": "from-dict", "command": "echo hi"}}
    )
    mngr_ctx = _make_mngr_ctx_with_plugins({PluginName("kanpan"): config})
    commands = _load_user_commands(mngr_ctx)
    assert commands["x"].name == "from-dict"


# =============================================================================
# Tests for _build_mark_palette
# =============================================================================


def test_build_mark_palette_returns_entries_for_markable_commands() -> None:
    commands = {"d": CustomCommand(name="delete", markable="light red"), "r": CustomCommand(name="refresh")}
    entries, attr_names = _build_mark_palette(commands)
    assert ("mark_d", "light red", "") in entries
    assert ("mark_d_focus", "light red,standout", "") in entries
    assert attr_names == ("mark_d",)


def test_build_mark_palette_uses_default_color_for_bare_true() -> None:
    commands = {"s": CustomCommand(name="stop", command="mngr stop $MNGR_AGENT_NAME", markable=True)}
    entries, attr_names = _build_mark_palette(commands)
    assert ("mark_s", "light cyan", "") in entries
    assert attr_names == ("mark_s",)


def test_build_mark_palette_skips_non_markable() -> None:
    commands = {"m": CustomCommand(name="mute")}
    entries, attr_names = _build_mark_palette(commands)
    assert entries == []
    assert attr_names == ()


# =============================================================================
# Tests for _show_transient_message, _restore_footer, _on_restore_footer
# =============================================================================


def test_show_transient_message_updates_footer() -> None:
    state = _make_state()
    _show_transient_message(state, "  Operation succeeded")
    assert state.footer_left_text.get_text()[0] == "  Operation succeeded"


def test_show_transient_message_with_loop_schedules_alarm() -> None:
    state = _make_state()
    state.loop = _make_mock_loop()
    _show_transient_message(state, "  Test message")
    assert state.loop._alarm_tracker.call_count == 1


def test_restore_footer_restores_steady_state() -> None:
    state = _make_state()
    state.steady_footer_text = "  Last refresh: 12:00:00"
    _show_transient_message(state, "  Temporary message")
    _restore_footer(state)
    assert state.footer_left_text.get_text()[0] == "  Last refresh: 12:00:00"


def test_on_restore_footer_callback_restores_footer() -> None:
    state = _make_state()
    state.steady_footer_text = "  Steady state text"
    _show_transient_message(state, "  Temporary")
    _on_restore_footer(_make_mock_loop(), state)
    assert state.footer_left_text.get_text()[0] == "  Steady state text"


# =============================================================================
# Tests for _dispatch_command (no-focus and no-loop paths)
# =============================================================================


def test_dispatch_command_refresh_without_loop_does_not_start_refresh() -> None:
    state = _make_state()
    _dispatch_command(state, "r", CustomCommand(name="refresh"))
    assert state.refresh_future is None


def test_dispatch_command_mute_without_focus_does_not_change_snapshot() -> None:
    state = _make_state(snapshot=_make_snapshot())
    original_snapshot = state.snapshot
    _dispatch_command(state, "m", CustomCommand(name="mute"))
    assert state.snapshot is original_snapshot


# =============================================================================
# Tests for _refresh_display
# =============================================================================


def test_refresh_display_rebuilds_body() -> None:
    state = _make_state(snapshot=_make_snapshot(entries=(_make_entry(name="agent-a"), _make_entry(name="agent-b"))))
    _refresh_display(state)
    assert state.list_walker is not None
    assert len(state.index_to_entry) == 2


def test_refresh_display_preserves_focus_by_name() -> None:
    entries = (_make_entry(name="agent-a"), _make_entry(name="agent-b"))
    state = _make_state(snapshot=_make_snapshot(entries=entries))
    _refresh_display(state)
    for idx, entry in state.index_to_entry.items():
        if entry.name == AgentName("agent-b"):
            state.list_walker.set_focus(idx)
            break
    _refresh_display(state)
    focused = _get_focused_entry(state)
    assert focused is not None
    assert focused.name == AgentName("agent-b")


# =============================================================================
# Tests for _on_spinner_tick
# =============================================================================


def test_on_spinner_tick_no_future_does_not_schedule() -> None:
    state = _make_state()
    state.refresh_future = None
    loop = _make_mock_loop()
    _on_spinner_tick(loop, state)
    assert loop._alarm_tracker.call_count == 0


def test_on_spinner_tick_not_done_animates() -> None:
    state = _make_state()
    state.refresh_future = Future()
    loop = _make_mock_loop()
    _on_spinner_tick(loop, state)
    assert "Refreshing" in state.footer_left_text.get_text()[0]
    assert loop._alarm_tracker.call_count == 1


def test_on_spinner_tick_done_finishes_refresh() -> None:
    snapshot = _make_snapshot(entries=(_make_entry(name="agent-a"),))
    state = _make_state(snapshot=snapshot)
    done_future: Future[BoardSnapshot] = Future()
    done_future.set_result(snapshot)
    state.refresh_future = done_future
    _on_spinner_tick(_make_mock_loop(), state)
    assert state.refresh_future is None


def test_schedule_next_refresh_sets_alarm() -> None:
    loop = _make_mock_loop()
    _schedule_next_refresh(loop, _make_state())
    assert loop._alarm_tracker.call_count == 1


# =============================================================================
# Tests for _finish_refresh
# =============================================================================


def test_finish_refresh_updates_snapshot_and_display() -> None:
    snapshot = _make_snapshot(entries=(_make_entry(name="agent-a"),))
    state = _make_state(snapshot=None)
    done_future: Future[BoardSnapshot] = Future()
    done_future.set_result(snapshot)
    state.refresh_future = done_future
    _finish_refresh(_make_mock_loop(), state)
    assert state.snapshot is not None
    assert state.refresh_future is None
    assert "Last refresh" in state.footer_left_text.get_text()[0]


def test_finish_refresh_handles_exception() -> None:
    old_snapshot = _make_snapshot(entries=(_make_entry(name="old-agent"),))
    state = _make_state(snapshot=old_snapshot)
    failed_future: Future[BoardSnapshot] = Future()
    failed_future.set_exception(RuntimeError("fetch failed"))
    state.refresh_future = failed_future
    _finish_refresh(_make_mock_loop(), state)
    assert state.refresh_future is None
    assert state.snapshot is not None
    assert any("Refresh failed" in e for e in state.snapshot.errors)


def test_finish_refresh_no_future_does_not_change_state() -> None:
    state = _make_state()
    state.refresh_future = None
    original_text = state.footer_left_text.get_text()[0]
    _finish_refresh(_make_mock_loop(), state)
    assert state.footer_left_text.get_text()[0] == original_text


# =============================================================================
# Tests for _on_custom_command_poll
# =============================================================================


def test_on_custom_command_poll_done_success() -> None:
    state = _make_state()
    future = _make_done_future(subprocess.CompletedProcess(args=[], returncode=0))
    cmd = CustomCommand(name="test-cmd", command="echo hello")
    _on_custom_command_poll(_make_mock_loop(), (state, future, cmd, AgentName("agent-a")))
    assert "test-cmd completed" in state.footer_left_text.get_text()[0]


def test_on_custom_command_poll_done_failure() -> None:
    state = _make_state()
    future = _make_done_future(subprocess.CompletedProcess(args=[], returncode=1, stderr="oops"))
    cmd = CustomCommand(name="test-cmd", command="echo hello")
    _on_custom_command_poll(_make_mock_loop(), (state, future, cmd, AgentName("agent-a")))
    assert "test-cmd failed" in state.footer_left_text.get_text()[0]


def test_on_custom_command_poll_done_exception() -> None:
    state = _make_state()
    future = _make_failed_future(RuntimeError("boom"))
    cmd = CustomCommand(name="test-cmd", command="echo hello")
    _on_custom_command_poll(_make_mock_loop(), (state, future, cmd, AgentName("agent-a")))
    assert "test-cmd failed" in state.footer_left_text.get_text()[0]


def test_on_custom_command_poll_not_done_animates() -> None:
    state = _make_state()
    future: Future[subprocess.CompletedProcess[str]] = Future()
    cmd = CustomCommand(name="test-cmd", command="echo hello")
    loop = _make_mock_loop()
    _on_custom_command_poll(loop, (state, future, cmd, AgentName("agent-a")))
    assert "Running test-cmd" in state.footer_left_text.get_text()[0]
    assert loop._alarm_tracker.call_count == 1


# =============================================================================
# Tests for _mute_focused_agent and _on_mute_persist_poll
# =============================================================================


def test_mute_focused_agent_no_focus_does_not_change_snapshot() -> None:
    state = _make_state(snapshot=_make_snapshot())
    original_snapshot = state.snapshot
    _mute_focused_agent(state)
    assert state.snapshot is original_snapshot


def test_mute_focused_agent_toggles_and_updates_ui() -> None:
    """Muting an agent optimistically updates the UI and creates an executor."""
    state = _make_state_with_focus(entries=(_make_entry(name="agent-to-mute", is_muted=False),))
    _mute_focused_agent(state)
    assert state.snapshot is not None
    assert state.snapshot.entries[0].is_muted is True
    assert "Muted" in state.footer_left_text.get_text()[0]
    assert state.executor is not None
    state.executor.shutdown(wait=False, cancel_futures=True)


def test_on_mute_persist_poll_success_does_not_revert() -> None:
    state = _make_state(snapshot=_make_snapshot(entries=(_make_entry(name="a", is_muted=True),)))
    future: Future[bool] = Future()
    future.set_result(True)
    _on_mute_persist_poll(_make_mock_loop(), (state, future, AgentName("a"), True))
    assert state.snapshot is not None
    assert state.snapshot.entries[0].is_muted is True


def test_on_mute_persist_poll_failure_reverts() -> None:
    state = _make_state(snapshot=_make_snapshot(entries=(_make_entry(name="a", is_muted=True),)))
    _refresh_display(state)
    future: Future[bool] = Future()
    future.set_exception(RuntimeError("persist failed"))
    _on_mute_persist_poll(_make_mock_loop(), (state, future, AgentName("a"), True))
    assert state.snapshot is not None
    assert state.snapshot.entries[0].is_muted is False


def test_on_mute_persist_poll_not_done_schedules_next() -> None:
    state = _make_state()
    future: Future[bool] = Future()
    loop = _make_mock_loop()
    _on_mute_persist_poll(loop, (state, future, AgentName("a"), True))
    assert loop._alarm_tracker.call_count == 1


# =============================================================================
# Tests for _run_shell_command
# =============================================================================


def test_run_shell_command_no_focus_does_not_create_executor() -> None:
    state = _make_state()
    _run_shell_command(state, CustomCommand(name="test", command="echo hello"))
    assert state.executor is None


def test_run_shell_command_with_focus_creates_executor() -> None:
    """Running a shell command on a focused agent starts the executor."""
    state = _make_state_with_focus(entries=(_make_entry(name="agent-a"),))
    cmd = CustomCommand(name="test-cmd", command="echo hello")
    _run_shell_command(state, cmd)
    assert "Running test-cmd" in state.footer_left_text.get_text()[0]
    assert state.executor is not None
    state.executor.shutdown(wait=True)


def test_dispatch_command_custom_shell_command() -> None:
    """Dispatching a custom command routes to shell execution."""
    state = _make_state_with_focus(entries=(_make_entry(name="agent-a"),))
    cmd = CustomCommand(name="custom", command="echo hello")
    _dispatch_command(state, "c", cmd)
    assert "Running custom" in state.footer_left_text.get_text()[0]
    assert state.executor is not None
    state.executor.shutdown(wait=True)


# =============================================================================
# Tests for _on_auto_refresh_alarm
# =============================================================================


def test_on_auto_refresh_alarm_skips_if_already_refreshing() -> None:
    state = _make_state()
    existing_future: Future[BoardSnapshot] = Future()
    state.refresh_future = existing_future
    _on_auto_refresh_alarm(_make_mock_loop(), state)
    assert state.refresh_future is existing_future


# =============================================================================
# Tests for _get_name_cell_markup with marks
# =============================================================================


def test_name_cell_markup_no_mark() -> None:
    entry = _make_entry()
    result = _get_name_cell_markup(entry)
    assert isinstance(result, str)
    assert result.startswith("  test-agent")


def test_name_cell_markup_delete_mark() -> None:
    entry = _make_entry()
    result = _get_name_cell_markup(entry, mark_key="d")
    assert isinstance(result, list)
    assert result[0] == ("mark_d", "d")
    assert "test-agent" in result[1]


def test_name_cell_markup_push_mark() -> None:
    entry = _make_entry(work_dir=Path("/tmp/work"))
    result = _get_name_cell_markup(entry, mark_key="p")
    assert isinstance(result, list)
    assert result[0] == ("mark_p", "p")


def test_name_cell_markup_custom_mark_uses_command_color() -> None:
    entry = _make_entry()
    result = _get_name_cell_markup(entry, mark_key="s")
    assert isinstance(result, list)
    assert result[0] == ("mark_s", "s")
    assert "test-agent" in result[1]


# =============================================================================
# Tests for _toggle_mark
# =============================================================================


def test_toggle_mark_adds_mark() -> None:
    state = _make_state_with_focus(entries=(_make_entry(),))
    _toggle_mark(state, "d")
    assert state.marks == {AgentName("test-agent"): "d"}


def test_toggle_mark_removes_same_mark() -> None:
    state = _make_state_with_focus(entries=(_make_entry(),))
    state.marks[AgentName("test-agent")] = "d"
    _toggle_mark(state, "d")
    assert AgentName("test-agent") not in state.marks


def test_toggle_mark_replaces_different_mark() -> None:
    state = _make_state_with_focus(entries=(_make_entry(work_dir=Path("/tmp/work")),))
    state.marks[AgentName("test-agent")] = "d"
    _toggle_mark(state, "p")
    assert state.marks[AgentName("test-agent")] == "p"


def test_toggle_push_mark_rejected_without_work_dir() -> None:
    state = _make_state_with_focus(entries=(_make_entry(),))
    _toggle_mark(state, "p")
    assert AgentName("test-agent") not in state.marks


# =============================================================================
# Tests for _unmark_focused and _unmark_all
# =============================================================================


def test_unmark_focused_removes_mark() -> None:
    state = _make_state_with_focus(entries=(_make_entry(),))
    state.marks[AgentName("test-agent")] = "d"
    _unmark_focused(state)
    assert AgentName("test-agent") not in state.marks


def test_unmark_focused_noop_without_mark() -> None:
    state = _make_state_with_focus(entries=(_make_entry(),))
    _unmark_focused(state)
    assert state.marks == {}


def test_unmark_all_clears_all_marks() -> None:
    e1 = _make_entry(name="agent-1")
    e2 = _make_entry(name="agent-2")
    state = _make_state_with_focus(entries=(e1, e2))
    state.marks[AgentName("agent-1")] = "d"
    state.marks[AgentName("agent-2")] = "p"
    _unmark_all(state)
    assert state.marks == {}


def test_unmark_all_noop_when_empty() -> None:
    state = _make_state_with_focus(entries=(_make_entry(),))
    _unmark_all(state)
    assert state.marks == {}


# =============================================================================
# Tests for _build_board_widgets with marks
# =============================================================================


def test_build_board_widgets_shows_mark_indicator() -> None:
    entry = _make_entry()
    snapshot = BoardSnapshot(entries=(entry,), repo_pr_loaded={}, fetch_time_seconds=0.1)
    marks = {AgentName("test-agent"): "d"}
    walker, index_to_entry = _build_board_widgets(snapshot, _BOARD_COLUMN_DEFS, marks)
    agent_idx = min(index_to_entry.keys())
    texts = _extract_text([walker[agent_idx]])
    assert len(texts) == 1
    assert texts[0].startswith("d")


# =============================================================================
# Tests for custom command dispatch (markable vs immediate)
# =============================================================================


def test_dispatch_markable_custom_command_toggles_mark() -> None:
    entry = _make_entry()
    cmd = CustomCommand(name="stop", command="mngr stop $MNGR_AGENT_NAME", markable=True)
    commands = {"s": cmd}
    state = _make_state_with_focus(entries=(entry,), commands=commands)
    _dispatch_command(state, "s", cmd)
    assert state.marks == {AgentName("test-agent"): "s"}


def test_dispatch_markable_custom_command_toggles_off() -> None:
    entry = _make_entry()
    cmd = CustomCommand(name="stop", command="mngr stop $MNGR_AGENT_NAME", markable=True)
    commands = {"s": cmd}
    state = _make_state_with_focus(entries=(entry,), commands=commands)
    state.marks[AgentName("test-agent")] = "s"
    _dispatch_command(state, "s", cmd)
    assert AgentName("test-agent") not in state.marks


def test_dispatch_immediate_custom_command_does_not_mark() -> None:
    entry = _make_entry()
    cmd = CustomCommand(name="connect", command="true")
    commands = {"c": cmd}
    state = _make_state_with_focus(entries=(entry,), commands=commands)
    _dispatch_command(state, "c", cmd)
    assert state.marks == {}


# =============================================================================
# Tests for _carry_forward_pr_data and _build_board_widgets PR failure handling
# =============================================================================


def test_carry_forward_pr_data_preserves_old_prs() -> None:
    repo_labels = ColumnData(labels={"remote": "git@github.com:org/repo.git"})
    pr = make_pr_info(number=42, head_branch="mngr/agent-1")
    old_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mngr/agent-1",
        pr=pr,
        create_pr_url=None,
        column_data=repo_labels,
    )
    old = BoardSnapshot(
        entries=(old_entry,),
        repo_pr_loaded={"org/repo": True},
        fetch_time_seconds=1.0,
    )

    new_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mngr/agent-1",
        pr=None,
        create_pr_url=None,
        column_data=repo_labels,
    )
    new = BoardSnapshot(
        entries=(new_entry,),
        errors=("gh auth failed",),
        repo_pr_loaded={},
        fetch_time_seconds=2.0,
    )

    result = _carry_forward_pr_data(old, new)
    assert result.repo_pr_loaded["org/repo"] is True
    assert result.entries[0].pr is not None
    assert result.entries[0].pr.number == 42
    assert "gh auth failed" in result.errors[0]
    assert result.fetch_time_seconds == 2.0


def test_carry_forward_pr_data_preserves_create_pr_url_without_pr() -> None:
    repo_labels = ColumnData(labels={"remote": "git@github.com:org/repo.git"})
    old_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mngr/agent-1",
        pr=None,
        create_pr_url="https://github.com/org/repo/compare/mngr/agent-1?expand=1",
        column_data=repo_labels,
    )
    old = BoardSnapshot(
        entries=(old_entry,),
        repo_pr_loaded={"org/repo": True},
        fetch_time_seconds=1.0,
    )

    new_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mngr/agent-1",
        pr=None,
        create_pr_url=None,
        column_data=repo_labels,
    )
    new = BoardSnapshot(
        entries=(new_entry,),
        repo_pr_loaded={},
        fetch_time_seconds=2.0,
    )

    result = _carry_forward_pr_data(old, new)
    assert result.repo_pr_loaded["org/repo"] is True
    assert result.entries[0].pr is None
    assert result.entries[0].create_pr_url == "https://github.com/org/repo/compare/mngr/agent-1?expand=1"


def test_carry_forward_pr_data_handles_new_agents() -> None:
    repo_labels = ColumnData(labels={"remote": "git@github.com:org/repo.git"})
    old = BoardSnapshot(
        entries=(),
        repo_pr_loaded={"org/repo": True},
        fetch_time_seconds=1.0,
    )

    new_entry = AgentBoardEntry(
        name=AgentName("agent-new"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mngr/agent-new",
        column_data=repo_labels,
    )
    new = BoardSnapshot(
        entries=(new_entry,),
        repo_pr_loaded={},
        fetch_time_seconds=2.0,
    )

    result = _carry_forward_pr_data(old, new)
    assert result.entries[0].pr is None


def test_first_load_pr_failure_shows_prs_not_loaded() -> None:
    repo_labels = ColumnData(labels={"remote": "git@github.com:org/repo.git"})
    entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mngr/agent-1",
        pr=None,
        create_pr_url=None,
        column_data=repo_labels,
    )
    snapshot = BoardSnapshot(
        entries=(entry,),
        errors=("gh pr list failed: auth required",),
        repo_pr_loaded={"org/repo": False},
        fetch_time_seconds=1.0,
    )
    walker, _ = _build_board_widgets(snapshot, _BOARD_COLUMN_DEFS)

    texts = _extract_text(list(walker))
    assert _text_contains(texts, "PRs not loaded")
    assert not _text_contains(texts, "no PR yet")
    assert not _text_contains(texts, "create PR")
    assert _text_contains(texts, "gh pr list failed")


def test_first_load_pr_success_shows_normal_heading() -> None:
    entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mngr/agent-1",
        pr=None,
        create_pr_url="https://github.com/org/repo/compare/mngr/agent-1?expand=1",
        column_data=ColumnData(labels={"remote": "git@github.com:org/repo.git"}),
    )
    snapshot = BoardSnapshot(
        entries=(entry,),
        repo_pr_loaded={"org/repo": True},
        fetch_time_seconds=1.0,
    )
    walker, _ = _build_board_widgets(snapshot, _BOARD_COLUMN_DEFS)

    texts = _extract_text(list(walker))
    assert _text_contains(texts, "no PR yet")
    assert not _text_contains(texts, "PRs not loaded")


def test_second_load_pr_failure_shows_carried_forward_prs() -> None:
    repo_labels = ColumnData(labels={"remote": "git@github.com:org/repo.git"})
    pr = make_pr_info(number=42, head_branch="mngr/agent-1")
    old_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mngr/agent-1",
        pr=pr,
        create_pr_url=None,
        column_data=repo_labels,
    )
    old = BoardSnapshot(
        entries=(old_entry,),
        repo_pr_loaded={"org/repo": True},
        fetch_time_seconds=1.0,
    )

    new_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mngr/agent-1",
        pr=None,
        create_pr_url=None,
        column_data=repo_labels,
    )
    new = BoardSnapshot(
        entries=(new_entry,),
        errors=("gh pr list failed: network error",),
        repo_pr_loaded={},
        fetch_time_seconds=2.0,
    )

    carried = _carry_forward_pr_data(old, new)
    walker, _ = _build_board_widgets(carried, _BOARD_COLUMN_DEFS)

    texts = _extract_text(list(walker))
    assert _text_contains(texts, "github.com/org/repo/pull/42")
    assert not _text_contains(texts, "PRs not loaded")
    assert not _text_contains(texts, "no PR yet")
    assert not _text_contains(texts, "create PR")
    assert _text_contains(texts, "network error")


def test_carry_forward_partial_failure_preserves_failed_repo_prs() -> None:
    """When one repo fails in a multi-repo refresh, carry forward only agents from the failed repo."""
    pr_good = make_pr_info(number=10, head_branch="mngr/good-agent")
    pr_bad = make_pr_info(number=20, head_branch="mngr/bad-agent")
    pr_good_refreshed = make_pr_info(number=10, head_branch="mngr/good-agent", state=PrState.MERGED)

    old_good_entry = AgentBoardEntry(
        name=AgentName("good-agent"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
        branch="mngr/good-agent",
        pr=pr_good,
        column_data=ColumnData(labels={"remote": "git@github.com:org/good.git"}),
    )
    old_bad_entry = AgentBoardEntry(
        name=AgentName("bad-agent"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
        branch="mngr/bad-agent",
        pr=pr_bad,
        column_data=ColumnData(labels={"remote": "git@github.com:org/bad.git"}),
    )
    old = BoardSnapshot(
        entries=(old_good_entry, old_bad_entry),
        repo_pr_loaded={"org/good": True, "org/bad": True},
        fetch_time_seconds=1.0,
    )

    # New snapshot: good repo succeeded (PR updated to MERGED), bad repo failed (PR is None)
    new_good_entry = AgentBoardEntry(
        name=AgentName("good-agent"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
        branch="mngr/good-agent",
        pr=pr_good_refreshed,
        column_data=ColumnData(labels={"remote": "git@github.com:org/good.git"}),
    )
    new_bad_entry = AgentBoardEntry(
        name=AgentName("bad-agent"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
        branch="mngr/bad-agent",
        pr=None,
        column_data=ColumnData(labels={"remote": "git@github.com:org/bad.git"}),
    )
    new = BoardSnapshot(
        entries=(new_good_entry, new_bad_entry),
        repo_pr_loaded={"org/good": True, "org/bad": False},
        fetch_time_seconds=2.0,
    )

    result = _carry_forward_pr_data(old, new)

    # Good agent keeps its fresh data (MERGED)
    assert result.entries[0].pr is not None
    assert result.entries[0].pr.state == PrState.MERGED

    # Bad agent gets its old PR carried forward
    assert result.entries[1].pr is not None
    assert result.entries[1].pr.number == 20

    assert result.repo_pr_loaded["org/good"] is True
    assert result.repo_pr_loaded["org/bad"] is True


# === Debounce / refresh tests ===


class _AlarmRecord(SimpleNamespace):
    """Record of a set_alarm_in call."""

    delay: float
    callback: object
    user_data: object


class _TestableLoop(MainLoop):
    """MainLoop subclass that records alarm operations instead of registering with the event loop."""

    def __init__(self) -> None:
        super().__init__(Text(""))
        self.alarms: list[_AlarmRecord] = []
        self.removed_alarms: list[object] = []
        self._next_handle = 0

    def set_alarm_in(self, sec: float, callback: Callable[..., Any], user_data: Any = None) -> int:
        handle = self._next_handle
        self._next_handle += 1
        self.alarms.append(_AlarmRecord(delay=sec, callback=callback, user_data=user_data))
        return handle

    def remove_alarm(self, handle: object) -> bool:
        self.removed_alarms.append(handle)
        return True


class _FakeExecutor:
    """Executor whose submit() always returns a pre-built future."""

    def __init__(self, future: Future[BoardSnapshot]) -> None:
        self._future = future

    def submit(self, fn: object, *args: object, **kwargs: object) -> Future[BoardSnapshot]:
        return self._future


def _make_dummy_snapshot(**overrides: Any) -> BoardSnapshot:
    """Build a BoardSnapshot with a single dummy entry to avoid empty-entries edge cases."""
    defaults: dict[str, Any] = {
        "entries": (
            AgentBoardEntry(
                name=AgentName("dummy"),
                state=AgentLifecycleState.DONE,
                provider_name=ProviderInstanceName("modal"),
            ),
        ),
        "repo_pr_loaded": {},
        "fetch_time_seconds": 0.1,
    }
    defaults.update(overrides)
    return BoardSnapshot(**defaults)


def _make_debounce_state(**overrides: Any) -> _KanpanState:
    """Build a _KanpanState with fake urwid widgets and sensible defaults for debounce tests."""
    defaults: dict[str, Any] = {
        "mngr_ctx": SimpleNamespace(config=SimpleNamespace(plugins={})),
        "frame": SimpleNamespace(body=None),
        "footer_left_text": SimpleNamespace(set_text=lambda text: None),
        "footer_left_attr": SimpleNamespace(set_attr_map=lambda m: None),
        "footer_right": SimpleNamespace(set_text=lambda text: None),
    }
    defaults.update(overrides)
    return _KanpanState.model_construct(**defaults)


def test_request_refresh_starts_immediately_when_cooldown_expired() -> None:
    loop = _TestableLoop()
    pre_built_future: Future[BoardSnapshot] = Future()
    pre_built_future.set_result(_make_dummy_snapshot())
    executor = _FakeExecutor(pre_built_future)
    state = _make_debounce_state(
        last_refresh_time=time.monotonic() - 100,
        executor=executor,
    )

    _request_refresh(loop, state, cooldown_seconds=5.0)

    assert state.refresh_future is pre_built_future


def test_request_refresh_defers_when_within_cooldown() -> None:
    loop = _TestableLoop()
    state = _make_debounce_state(last_refresh_time=time.monotonic())

    _request_refresh(loop, state, cooldown_seconds=60.0)

    assert state.refresh_future is None
    assert state.deferred_refresh_alarm is not None
    assert len(loop.alarms) == 1
    delay = loop.alarms[0].delay
    assert 59.0 < delay <= 60.0


def test_request_refresh_replaces_deferred_with_sooner_alarm() -> None:
    """A manual refresh (short cooldown) should replace a pending auto refresh (long cooldown)."""
    loop = _TestableLoop()
    now = time.monotonic()
    state = _make_debounce_state(
        last_refresh_time=now - 2,
        deferred_refresh_alarm=999,
        deferred_refresh_fire_at=now + 58,
    )

    _request_refresh(loop, state, cooldown_seconds=5.0)

    assert 999 in loop.removed_alarms
    assert state.deferred_refresh_alarm is not None
    assert len(loop.alarms) == 1
    delay = loop.alarms[0].delay
    assert 2.0 < delay <= 3.0


def test_request_refresh_keeps_existing_if_sooner() -> None:
    """An auto refresh request should not replace a sooner pending manual refresh."""
    loop = _TestableLoop()
    now = time.monotonic()
    state = _make_debounce_state(
        last_refresh_time=now - 2,
        deferred_refresh_alarm=777,
        deferred_refresh_fire_at=now + 3,
    )

    _request_refresh(loop, state, cooldown_seconds=60.0)

    assert len(loop.removed_alarms) == 0
    assert len(loop.alarms) == 0
    assert state.deferred_refresh_alarm == 777


def test_request_refresh_noop_when_already_refreshing() -> None:
    loop = _TestableLoop()
    existing_future: Future[BoardSnapshot] = Future()
    state = _make_debounce_state(refresh_future=existing_future)

    _request_refresh(loop, state, cooldown_seconds=0.0)

    assert state.refresh_future is existing_future
    assert len(loop.alarms) == 0


def test_finish_refresh_schedules_normal_interval_on_success() -> None:
    loop = _TestableLoop()
    snapshot = _make_dummy_snapshot(fetch_time_seconds=1.0)
    future: Future[BoardSnapshot] = Future()
    future.set_result(snapshot)
    state = _make_debounce_state(refresh_future=future)

    _finish_refresh(loop, state)

    assert state.snapshot == snapshot
    assert state.refresh_future is None
    auto_refresh_alarms = [a for a in loop.alarms if a.delay == DEFAULT_REFRESH_INTERVAL_SECONDS]
    assert len(auto_refresh_alarms) == 1


def test_finish_refresh_uses_auto_cooldown_on_failure() -> None:
    """After a failed refresh, the next refresh should be deferred by retry_cooldown_seconds."""
    loop = _TestableLoop()
    future: Future[BoardSnapshot] = Future()
    future.set_exception(RuntimeError("GitHub API error"))
    state = _make_debounce_state(
        refresh_future=future,
        retry_cooldown_seconds=30.0,
    )

    _finish_refresh(loop, state)

    assert state.refresh_future is None
    assert state.deferred_refresh_alarm is not None
    assert len(loop.alarms) == 1
    delay = loop.alarms[0].delay
    assert 29.0 < delay <= 30.0


# === local-only refresh ===


def test_local_refresh_does_not_reset_last_refresh_time() -> None:
    """A local-only refresh should not update last_refresh_time."""
    loop = _TestableLoop()
    snapshot = _make_dummy_snapshot()
    future: Future[BoardSnapshot] = Future()
    future.set_result(snapshot)
    original_time = 1000.0
    state = _make_debounce_state(
        refresh_future=future,
        refresh_is_local_only=True,
        last_refresh_time=original_time,
    )

    _finish_refresh(loop, state)

    assert state.last_refresh_time == original_time
    assert state.refresh_future is None
    assert state.refresh_is_local_only is False


def test_local_refresh_does_not_schedule_next_auto_refresh() -> None:
    """A local-only refresh should not schedule the next periodic auto-refresh."""
    loop = _TestableLoop()
    snapshot = _make_dummy_snapshot()
    future: Future[BoardSnapshot] = Future()
    future.set_result(snapshot)
    state = _make_debounce_state(
        refresh_future=future,
        refresh_is_local_only=True,
    )

    _finish_refresh(loop, state)

    auto_refresh_alarms = [a for a in loop.alarms if a.delay == DEFAULT_REFRESH_INTERVAL_SECONDS]
    assert len(auto_refresh_alarms) == 0


def test_start_local_refresh_noop_when_already_refreshing() -> None:
    """_start_local_refresh should do nothing if a refresh is already in flight."""
    loop = _TestableLoop()
    existing_future: Future[BoardSnapshot] = Future()
    state = _make_debounce_state(refresh_future=existing_future)

    _start_local_refresh(loop, state)

    assert state.refresh_future is existing_future
    assert len(loop.alarms) == 0


# =============================================================================
# Tests for custom column text/markup functions
# =============================================================================


def test_custom_col_text_reads_from_labels() -> None:
    entry = _make_entry(labels={"blocked": "yes"})
    assert _custom_col_text(entry, "blocked", None, None) == "yes"


def test_custom_col_text_returns_empty_for_missing_label() -> None:
    entry = _make_entry()
    assert _custom_col_text(entry, "blocked", None, None) == ""


def test_custom_col_text_reads_from_plugin_data() -> None:
    entry = _make_entry(plugin_data={"claude": {"waiting_reason": "PERMISSIONS"}})
    assert _custom_col_text(entry, "waiting", "claude", "waiting_reason", source="agent") == "PERMISSIONS"


def test_custom_col_text_returns_empty_for_missing_plugin_data() -> None:
    entry = _make_entry()
    assert _custom_col_text(entry, "waiting", "claude", "waiting_reason", source="agent") == ""


def test_custom_col_text_returns_empty_for_missing_plugin_field() -> None:
    entry = _make_entry(plugin_data={"claude": {}})
    assert _custom_col_text(entry, "waiting", "claude", "waiting_reason", source="agent") == ""


def test_custom_col_markup_applies_color_when_configured() -> None:
    entry = _make_entry(labels={"blocked": "yes"})
    result = _custom_col_markup(entry, "blocked", None, None, {"yes": "light red"}, source="labels")
    assert result == ("col_blocked_yes", "yes")


def test_custom_col_markup_no_color_returns_plain_text() -> None:
    entry = _make_entry(labels={"blocked": "maybe"})
    result = _custom_col_markup(entry, "blocked", None, None, {"yes": "light red"}, source="labels")
    assert result == "maybe"


def test_custom_col_markup_empty_value_returns_empty_string() -> None:
    entry = _make_entry()
    result = _custom_col_markup(entry, "blocked", None, None, {"yes": "light red"}, source="labels")
    assert result == ""


def test_custom_col_markup_plugin_data_with_color() -> None:
    entry = _make_entry(plugin_data={"claude": {"reason": "PERMISSIONS"}})
    result = _custom_col_markup(entry, "wait", "claude", "reason", {"PERMISSIONS": "light red"}, source="agent")
    assert result == ("col_wait_PERMISSIONS", "PERMISSIONS")


# =============================================================================
# Tests for _build_custom_column_defs
# =============================================================================


def test_build_custom_column_defs_label_source() -> None:
    config = {"blocked": CustomColumnConfig(header="BLOCKED", colors={"yes": "light red"})}
    defs = _build_custom_column_defs(config)
    assert len(defs) == 1
    assert defs[0].name == "custom_blocked"
    assert defs[0].header == "BLOCKED"
    assert defs[0].flexible is False


def test_build_custom_column_defs_agent_source_with_plugin_data() -> None:
    config = {
        "wait": CustomColumnConfig(header="WAIT", source="agent", plugin_name="claude", field="waiting_reason"),
    }
    defs = _build_custom_column_defs(config)
    assert len(defs) == 1
    assert defs[0].name == "custom_wait"
    entry = _make_entry(plugin_data={"claude": {"waiting_reason": "PERMISSIONS"}})
    assert defs[0].text_fn(entry) == "PERMISSIONS"


def test_build_custom_column_defs_agent_source() -> None:
    config = {
        "cost": CustomColumnConfig(header="COST", source="agent", plugin_name="claude", field="total_cost"),
    }
    defs = _build_custom_column_defs(config)
    entry = _make_entry(plugin_data={"claude": {"total_cost": "1.50"}})
    assert defs[0].text_fn(entry) == "1.50"


def test_build_custom_column_defs_text_fn_reads_labels() -> None:
    config = {"blocked": CustomColumnConfig(header="BLOCKED")}
    defs = _build_custom_column_defs(config)
    entry = _make_entry(labels={"blocked": "unblocked"})
    assert defs[0].text_fn(entry) == "unblocked"


def test_build_custom_column_defs_markup_fn_applies_color() -> None:
    config = {"blocked": CustomColumnConfig(header="BLOCKED", colors={"yes": "light red"})}
    defs = _build_custom_column_defs(config)
    entry = _make_entry(labels={"blocked": "yes"})
    assert defs[0].markup_fn(entry) == ("col_blocked_yes", "yes")


def test_build_custom_column_defs_empty_config() -> None:
    assert _build_custom_column_defs({}) == []


# =============================================================================
# Tests for _assemble_column_defs
# =============================================================================


def test_assemble_column_defs_no_custom_no_order_returns_builtins() -> None:
    result = _assemble_column_defs(_BOARD_COLUMN_DEFS, [], None)
    assert result == _BOARD_COLUMN_DEFS


def test_assemble_column_defs_custom_inserted_before_link() -> None:
    config = {"blocked": CustomColumnConfig(header="BLOCKED")}
    custom = _build_custom_column_defs(config)
    result = _assemble_column_defs(_BOARD_COLUMN_DEFS, custom, None)
    names = [d.name for d in result]
    assert names[-1] == "link"
    assert names[-2] == "custom_blocked"
    assert result[-1].flexible is True


def test_assemble_column_defs_explicit_order() -> None:
    config = {"blocked": CustomColumnConfig(header="BLOCKED")}
    custom = _build_custom_column_defs(config)
    result = _assemble_column_defs(_BOARD_COLUMN_DEFS, custom, ["name", "custom_blocked", "state", "link"])
    names = [d.name for d in result]
    assert names == ["name", "custom_blocked", "state", "link"]
    assert result[-1].flexible is True
    assert all(not d.flexible for d in result[:-1])


def test_assemble_column_defs_explicit_order_skips_unknown() -> None:
    result = _assemble_column_defs(_BOARD_COLUMN_DEFS, [], ["name", "nonexistent", "link"])
    names = [d.name for d in result]
    assert names == ["name", "link"]


def test_assemble_column_defs_empty_order_falls_back_to_builtins() -> None:
    result = _assemble_column_defs(_BOARD_COLUMN_DEFS, [], [])
    assert result == _BOARD_COLUMN_DEFS


# =============================================================================
# Tests for _build_column_palette
# =============================================================================


def test_build_column_palette_returns_entries_and_attrs() -> None:
    config = {
        "blocked": CustomColumnConfig(header="BLOCKED", colors={"yes": "light red", "no": "light green"}),
    }
    entries, attr_names = _build_column_palette(config)
    assert ("col_blocked_yes", "light red", "") in entries
    assert ("col_blocked_yes_focus", "light red,standout", "") in entries
    assert ("col_blocked_no", "light green", "") in entries
    assert ("col_blocked_no_focus", "light green,standout", "") in entries
    assert "col_blocked_yes" in attr_names
    assert "col_blocked_no" in attr_names


def test_build_column_palette_empty_config() -> None:
    entries, attr_names = _build_column_palette({})
    assert entries == []
    assert attr_names == ()


def test_build_column_palette_no_colors() -> None:
    config = {"blocked": CustomColumnConfig(header="BLOCKED")}
    entries, attr_names = _build_column_palette(config)
    assert entries == []
    assert attr_names == ()


# =============================================================================
# Tests for _load_user_columns
# =============================================================================


def test_load_user_columns_no_config_returns_empty() -> None:
    assert _load_user_columns(_make_mngr_ctx_with_plugins()) == {}


def test_load_user_columns_handles_dict_values() -> None:
    config = KanpanPluginConfig.model_construct(
        enabled=True,
        commands={},
        columns={"blocked": {"header": "BLOCKED", "colors": {"yes": "light red"}}},
    )
    mngr_ctx = _make_mngr_ctx_with_plugins({PluginName("kanpan"): config})
    columns = _load_user_columns(mngr_ctx)
    assert "blocked" in columns
    assert columns["blocked"].header == "BLOCKED"
    assert columns["blocked"].colors == {"yes": "light red"}


def test_load_user_columns_handles_proper_objects() -> None:
    config = KanpanPluginConfig(
        columns={"blocked": CustomColumnConfig(header="BLOCKED")},
    )
    mngr_ctx = _make_mngr_ctx_with_plugins({PluginName("kanpan"): config})
    columns = _load_user_columns(mngr_ctx)
    assert columns["blocked"].header == "BLOCKED"


# =============================================================================
# Tests for custom columns in board rendering
# =============================================================================


def test_build_board_widgets_with_custom_column_renders_values() -> None:
    config = {"blocked": CustomColumnConfig(header="BLOCKED")}
    custom_defs = _build_custom_column_defs(config)
    column_defs = _assemble_column_defs(_BOARD_COLUMN_DEFS, custom_defs, None)
    entries = (_make_entry(name="agent-1", labels={"blocked": "yes"}),)
    walker, _ = _build_board_widgets(_make_snapshot(entries=entries), column_defs)
    texts = _extract_text(list(walker))
    assert _text_contains(texts, "BLOCKED")
    assert _text_contains(texts, "yes")


def test_build_board_widgets_custom_column_empty_when_no_label() -> None:
    config = {"blocked": CustomColumnConfig(header="BLOCKED")}
    custom_defs = _build_custom_column_defs(config)
    column_defs = _assemble_column_defs(_BOARD_COLUMN_DEFS, custom_defs, None)
    entries = (_make_entry(name="agent-1"),)
    walker, _ = _build_board_widgets(_make_snapshot(entries=entries), column_defs)
    texts = _extract_text(list(walker))
    assert _text_contains(texts, "BLOCKED")


def test_build_board_widgets_muted_agent_flattens_custom_column_colors() -> None:
    config = {"blocked": CustomColumnConfig(header="BLOCKED", colors={"yes": "light red"})}
    custom_defs = _build_custom_column_defs(config)
    column_defs = _assemble_column_defs(_BOARD_COLUMN_DEFS, custom_defs, None)
    entries = (_make_entry(name="agent-1", labels={"blocked": "yes"}, is_muted=True),)
    walker, index_to_entry = _build_board_widgets(_make_snapshot(entries=entries), column_defs)
    agent_idx = next(iter(index_to_entry.keys()))
    row_widget = walker[agent_idx]
    assert isinstance(row_widget, AttrMap)
    inner_row = row_widget.original_widget
    for child, _options in inner_row.contents:
        if isinstance(child, Text):
            markup = child.get_text()
            if "yes" in str(markup[0]):
                assert markup[1][0][0] == "muted"


def test_build_board_widgets_custom_column_focus_map_includes_col_attrs() -> None:
    config = {"blocked": CustomColumnConfig(header="BLOCKED", colors={"yes": "light red"})}
    custom_defs = _build_custom_column_defs(config)
    column_defs = _assemble_column_defs(_BOARD_COLUMN_DEFS, custom_defs, None)
    col_palette, col_attr_names = _build_column_palette(config)
    entries = (_make_entry(name="agent-1", labels={"blocked": "yes"}),)
    walker, index_to_entry = _build_board_widgets(
        _make_snapshot(entries=entries), column_defs, col_attr_names=col_attr_names
    )
    agent_idx = next(iter(index_to_entry.keys()))
    attr_map_widget = walker[agent_idx]
    assert isinstance(attr_map_widget, AttrMap)
    assert attr_map_widget.focus_map is not None
    assert "col_blocked_yes" in attr_map_widget.focus_map
    assert attr_map_widget.focus_map["col_blocked_yes"] == "col_blocked_yes_focus"


# =============================================================================
# _load_refresh_hooks
# =============================================================================


def test_load_refresh_hooks_with_refresh_hook_instances() -> None:
    hooks_raw: dict[str, Any] = {
        "a": RefreshHook(name="Hook A", command="cmd-a"),
        "b": RefreshHook(name="Hook B", command="cmd-b"),
    }
    result = _load_refresh_hooks(hooks_raw)
    assert len(result) == 2
    assert result[0].name == "Hook A"
    assert result[1].name == "Hook B"


def test_load_refresh_hooks_with_raw_dicts() -> None:
    hooks_raw: dict[str, Any] = {
        "a": {"name": "Hook A", "command": "cmd-a"},
    }
    result = _load_refresh_hooks(hooks_raw)
    assert len(result) == 1
    assert result[0].name == "Hook A"
    assert result[0].command == "cmd-a"
    assert result[0].enabled is True


def test_load_refresh_hooks_filters_disabled() -> None:
    hooks_raw: dict[str, Any] = {
        "a": RefreshHook(name="Enabled", command="cmd-a", enabled=True),
        "b": RefreshHook(name="Disabled", command="cmd-b", enabled=False),
        "c": {"name": "Also disabled", "command": "cmd-c", "enabled": False},
    }
    result = _load_refresh_hooks(hooks_raw)
    assert len(result) == 1
    assert result[0].name == "Enabled"


def test_load_refresh_hooks_skips_invalid_values() -> None:
    hooks_raw: dict[str, Any] = {
        "a": RefreshHook(name="Valid", command="cmd-a"),
        "b": 42,
        "c": "not a hook",
    }
    result = _load_refresh_hooks(hooks_raw)
    assert len(result) == 1
    assert result[0].name == "Valid"


def test_load_refresh_hooks_empty_input() -> None:
    result = _load_refresh_hooks({})
    assert result == []
