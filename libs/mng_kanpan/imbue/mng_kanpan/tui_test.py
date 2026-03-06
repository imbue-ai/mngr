"""Unit tests for the kanpan TUI."""

import subprocess
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from urwid.event_loop.abstract_loop import ExitMainLoop
from urwid.widget.attr_map import AttrMap
from urwid.widget.columns import Columns
from urwid.widget.text import Text

from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import PluginName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng_kanpan.data_types import AgentBoardEntry
from imbue.mng_kanpan.data_types import BoardSection
from imbue.mng_kanpan.data_types import BoardSnapshot
from imbue.mng_kanpan.data_types import CheckStatus
from imbue.mng_kanpan.data_types import CustomCommand
from imbue.mng_kanpan.data_types import KanpanPluginConfig
from imbue.mng_kanpan.data_types import PrInfo
from imbue.mng_kanpan.data_types import PrState
from imbue.mng_kanpan.testing import make_pr_info
from imbue.mng_kanpan.tui import _KanpanInputHandler
from imbue.mng_kanpan.tui import _KanpanState
from imbue.mng_kanpan.tui import _build_board_widgets
from imbue.mng_kanpan.tui import _build_command_map
from imbue.mng_kanpan.tui import _cancel_delete
from imbue.mng_kanpan.tui import _carry_forward_pr_data
from imbue.mng_kanpan.tui import _classify_entry
from imbue.mng_kanpan.tui import _clear_focus
from imbue.mng_kanpan.tui import _confirm_delete
from imbue.mng_kanpan.tui import _delete_focused_agent
from imbue.mng_kanpan.tui import _dispatch_command
from imbue.mng_kanpan.tui import _finish_delete
from imbue.mng_kanpan.tui import _finish_push
from imbue.mng_kanpan.tui import _finish_refresh
from imbue.mng_kanpan.tui import _format_section_heading
from imbue.mng_kanpan.tui import _get_focused_entry
from imbue.mng_kanpan.tui import _get_state_attr
from imbue.mng_kanpan.tui import _is_focus_on_first_selectable
from imbue.mng_kanpan.tui import _is_safe_to_delete
from imbue.mng_kanpan.tui import _load_user_commands
from imbue.mng_kanpan.tui import _mute_focused_agent
from imbue.mng_kanpan.tui import _on_auto_refresh_alarm
from imbue.mng_kanpan.tui import _on_custom_command_poll
from imbue.mng_kanpan.tui import _on_delete_poll
from imbue.mng_kanpan.tui import _on_mute_persist_poll
from imbue.mng_kanpan.tui import _on_push_poll
from imbue.mng_kanpan.tui import _on_restore_footer
from imbue.mng_kanpan.tui import _on_spinner_tick
from imbue.mng_kanpan.tui import _push_focused_agent
from imbue.mng_kanpan.tui import _refresh_display
from imbue.mng_kanpan.tui import _restore_footer
from imbue.mng_kanpan.tui import _run_shell_command
from imbue.mng_kanpan.tui import _schedule_next_refresh
from imbue.mng_kanpan.tui import _show_transient_message
from imbue.mng_kanpan.tui import _update_snapshot_mute

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
    work_dir: Path | None = None,
    commits_ahead: int | None = None,
    create_pr_url: str | None = None,
    is_muted: bool = False,
) -> AgentBoardEntry:
    return AgentBoardEntry(
        name=AgentName(name),
        state=state,
        provider_name=ProviderInstanceName("local"),
        work_dir=work_dir,
        pr=pr,
        commits_ahead=commits_ahead,
        create_pr_url=create_pr_url,
        is_muted=is_muted,
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
        head_branch="mng/test-agent",
        check_status=check_status,
        is_draft=False,
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
    footer_right = Text("r: refresh  q: quit")
    frame = SimpleNamespace(body=None)
    mng_ctx = SimpleNamespace(config=SimpleNamespace(plugins={}))
    return _KanpanState.model_construct(
        mng_ctx=mng_ctx,
        snapshot=snapshot,
        frame=frame,
        footer_left_text=footer_left_text,
        footer_left_attr=footer_left_attr,
        footer_right=footer_right,
        commands=commands or {},
        spinner_index=0,
        refresh_future=None,
        delete_future=None,
        deleting_agent_name=None,
        pending_delete_name=None,
        push_future=None,
        pushing_agent_name=None,
        executor=None,
        index_to_entry={},
        list_walker=None,
        focused_agent_name=None,
        steady_footer_text="  Loading...",
    )


def _make_state_with_focus(
    entries: tuple[AgentBoardEntry, ...],
    commands: dict[str, CustomCommand] | None = None,
) -> _KanpanState:
    """Create a state with board widgets built and focus on the first agent."""
    snapshot = _make_snapshot(entries=entries)
    state = _make_state(snapshot=snapshot, commands=commands)
    walker, index_to_entry = _build_board_widgets(snapshot)
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


# =============================================================================
# Tests for _classify_entry
# =============================================================================


def test_classify_entry_muted_always_goes_to_muted_section() -> None:
    entry = _make_entry(is_muted=True, pr=_make_pr(state=PrState.MERGED))
    assert _classify_entry(entry) == BoardSection.MUTED


def test_classify_entry_no_pr_is_still_cooking() -> None:
    assert _classify_entry(_make_entry(pr=None)) == BoardSection.STILL_COOKING


def test_classify_entry_merged_pr() -> None:
    assert _classify_entry(_make_entry(pr=_make_pr(state=PrState.MERGED))) == BoardSection.PR_MERGED


def test_classify_entry_closed_pr() -> None:
    assert _classify_entry(_make_entry(pr=_make_pr(state=PrState.CLOSED))) == BoardSection.PR_CLOSED


def test_classify_entry_open_pr() -> None:
    assert _classify_entry(_make_entry(pr=_make_pr(state=PrState.OPEN))) == BoardSection.PR_BEING_REVIEWED


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
# Tests for _is_safe_to_delete
# =============================================================================


def test_is_safe_to_delete_merged_pr() -> None:
    assert _is_safe_to_delete(_make_entry(pr=_make_pr(state=PrState.MERGED))) is True


def test_is_safe_to_delete_open_pr() -> None:
    assert _is_safe_to_delete(_make_entry(pr=_make_pr(state=PrState.OPEN))) is False


def test_is_safe_to_delete_no_pr() -> None:
    assert _is_safe_to_delete(_make_entry(pr=None)) is False


# =============================================================================
# Tests for _build_board_widgets
# =============================================================================


def test_build_board_widgets_none_snapshot_shows_loading() -> None:
    walker, _ = _build_board_widgets(None)
    assert len(walker) == 1
    assert isinstance(walker[0], Text)


def test_build_board_widgets_empty_snapshot_shows_no_agents() -> None:
    walker, _ = _build_board_widgets(_make_snapshot())
    assert len(walker) == 1
    assert "No agents found" in str(walker[0].get_text()[0])


def test_build_board_widgets_with_entries_creates_sections() -> None:
    entries = (_make_entry(name="cooking"), _make_entry(name="merged", pr=_make_pr(state=PrState.MERGED)))
    walker, index_to_entry = _build_board_widgets(_make_snapshot(entries=entries))
    assert len(walker) >= 4
    assert len(index_to_entry) == 2


def test_build_board_widgets_populates_index_to_entry() -> None:
    entries = (_make_entry(name="agent-a"), _make_entry(name="agent-b"))
    _, index_to_entry = _build_board_widgets(_make_snapshot(entries=entries))
    names = {entry.name for entry in index_to_entry.values()}
    assert AgentName("agent-a") in names
    assert AgentName("agent-b") in names


def test_build_board_widgets_with_errors_shows_them() -> None:
    walker, _ = _build_board_widgets(_make_snapshot(errors=("Something went wrong",)))
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


def test_input_handler_uppercase_q_exits() -> None:
    with pytest.raises(ExitMainLoop):
        _KanpanInputHandler(state=_make_state())("Q")


def test_input_handler_ctrl_c_exits() -> None:
    with pytest.raises(ExitMainLoop):
        _KanpanInputHandler(state=_make_state())("ctrl c")


def test_input_handler_ignores_mouse_events() -> None:
    state = _make_state()
    result = _KanpanInputHandler(state=state)(("mouse press", 1, 0, 0))
    assert result is None
    assert state.pending_delete_name is None


def test_input_handler_passes_through_navigation_keys() -> None:
    handler = _KanpanInputHandler(state=_make_state())
    for key in ("down", "page up", "page down", "home", "end"):
        assert handler(key) is None


def test_input_handler_swallows_unknown_keys() -> None:
    assert _KanpanInputHandler(state=_make_state())("x") is True


def test_input_handler_pending_delete_y_confirms() -> None:
    state = _make_state()
    state.pending_delete_name = AgentName("doomed-agent")
    result = _KanpanInputHandler(state=state)("y")
    assert result is True
    assert state.pending_delete_name is None


def test_input_handler_pending_delete_other_key_cancels() -> None:
    state = _make_state()
    state.pending_delete_name = AgentName("doomed-agent")
    state.steady_footer_text = "  Steady"
    result = _KanpanInputHandler(state=state)("n")
    assert result is True
    assert state.pending_delete_name is None
    assert state.footer_left_text.get_text()[0] == "  Steady"


def test_input_handler_up_on_first_selectable_clears_focus() -> None:
    state = _make_state_with_focus(entries=(_make_entry(name="agent-a"),))
    result = _KanpanInputHandler(state=state)("up")
    assert result is True
    assert state.focused_agent_name is None


# =============================================================================
# Tests for _build_command_map and _load_user_commands
# =============================================================================


def _make_mng_ctx_with_plugins(
    plugins: dict[PluginName, object] | None = None,
) -> Any:
    """Create a SimpleNamespace that mimics MngContext.get_plugin_config behavior."""
    plugin_dict = plugins or {}

    def get_plugin_config(name: str, config_type: type) -> object:  # ty: ignore[invalid-argument-type]
        config = plugin_dict.get(PluginName(name))
        if config is None:
            return config_type()
        return config

    return SimpleNamespace(
        config=SimpleNamespace(plugins=plugin_dict),
        get_plugin_config=get_plugin_config,
    )


def test_build_command_map_returns_builtins_with_no_user_config() -> None:
    commands = _build_command_map(_make_mng_ctx_with_plugins())  # ty: ignore[invalid-argument-type]
    assert {"r", "p", "d", "m"} <= set(commands.keys())


def test_build_command_map_user_command_overrides_builtin() -> None:
    mng_ctx = _make_mng_ctx_with_plugins(
        {
            PluginName("kanpan"): KanpanPluginConfig(
                commands={"r": CustomCommand(name="custom-refresh", command="echo refresh")}
            )
        }
    )
    commands = _build_command_map(mng_ctx)  # ty: ignore[invalid-argument-type]
    assert commands["r"].name == "custom-refresh"


def test_build_command_map_disabled_command_is_excluded() -> None:
    mng_ctx = _make_mng_ctx_with_plugins(
        {PluginName("kanpan"): KanpanPluginConfig(commands={"d": CustomCommand(name="delete", enabled=False)})}
    )
    assert "d" not in _build_command_map(mng_ctx)  # ty: ignore[invalid-argument-type]


def test_load_user_commands_no_kanpan_plugin_returns_empty() -> None:
    assert _load_user_commands(_make_mng_ctx_with_plugins()) == {}  # ty: ignore[invalid-argument-type]


def test_load_user_commands_handles_dict_values() -> None:
    config = KanpanPluginConfig.model_construct(
        enabled=True, commands={"x": {"name": "from-dict", "command": "echo hi"}}
    )
    mng_ctx = _make_mng_ctx_with_plugins({PluginName("kanpan"): config})
    commands = _load_user_commands(mng_ctx)  # ty: ignore[invalid-argument-type]
    assert commands["x"].name == "from-dict"


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
# Tests for _cancel_delete and _confirm_delete
# =============================================================================


def test_cancel_delete_clears_pending_and_restores_footer() -> None:
    state = _make_state()
    state.pending_delete_name = AgentName("agent-to-cancel")
    state.steady_footer_text = "  Steady state"
    _cancel_delete(state)
    assert state.pending_delete_name is None
    assert state.footer_left_text.get_text()[0] == "  Steady state"


def test_confirm_delete_clears_pending() -> None:
    state = _make_state()
    state.pending_delete_name = AgentName("agent-to-delete")
    _confirm_delete(state)
    assert state.pending_delete_name is None


def test_confirm_delete_with_none_pending_does_not_create_executor() -> None:
    state = _make_state()
    state.pending_delete_name = None
    _confirm_delete(state)
    assert state.executor is None


# =============================================================================
# Tests for _dispatch_command (no-focus and no-loop paths)
# =============================================================================


def test_dispatch_command_refresh_without_loop_does_not_start_refresh() -> None:
    state = _make_state()
    _dispatch_command(state, "r", CustomCommand(name="refresh"))
    assert state.refresh_future is None


def test_dispatch_command_delete_without_focus_does_not_set_pending() -> None:
    state = _make_state()
    _dispatch_command(state, "d", CustomCommand(name="delete"))
    assert state.pending_delete_name is None
    assert state.delete_future is None


def test_dispatch_command_push_without_focus_does_not_start_push() -> None:
    state = _make_state()
    _dispatch_command(state, "p", CustomCommand(name="push"))
    assert state.push_future is None


def test_dispatch_command_mute_without_focus_does_not_change_snapshot() -> None:
    state = _make_state(snapshot=_make_snapshot())
    original_snapshot = state.snapshot
    _dispatch_command(state, "m", CustomCommand(name="mute"))
    assert state.snapshot is original_snapshot


# =============================================================================
# Tests for _delete_focused_agent
# =============================================================================


def test_delete_focused_agent_already_deleting_does_not_start_new() -> None:
    state = _make_state()
    existing_future: Future[subprocess.CompletedProcess[str]] = Future()
    state.delete_future = existing_future
    _delete_focused_agent(state)
    assert state.delete_future is existing_future


def test_delete_focused_agent_no_focus_does_not_set_pending() -> None:
    state = _make_state()
    _delete_focused_agent(state)
    assert state.pending_delete_name is None
    assert state.delete_future is None


def test_delete_focused_agent_non_merged_prompts_confirmation() -> None:
    state = _make_state_with_focus(entries=(_make_entry(name="unmerged-agent", pr=_make_pr(state=PrState.OPEN)),))
    _delete_focused_agent(state)
    assert state.pending_delete_name == AgentName("unmerged-agent")
    assert "confirm" in state.footer_left_text.get_text()[0].lower()


def test_delete_focused_agent_merged_pr_executes_immediately() -> None:
    """Safe-to-delete agents (merged PR) skip confirmation and start delete."""
    state = _make_state_with_focus(entries=(_make_entry(name="merged-agent", pr=_make_pr(state=PrState.MERGED)),))
    _delete_focused_agent(state)
    assert state.pending_delete_name is None
    assert state.deleting_agent_name == AgentName("merged-agent")
    assert state.delete_future is not None
    assert state.executor is not None
    state.executor.shutdown(wait=False, cancel_futures=True)


# =============================================================================
# Tests for _push_focused_agent
# =============================================================================


def test_push_focused_agent_already_pushing_does_not_start_new() -> None:
    state = _make_state()
    existing_future: Future[subprocess.CompletedProcess[str]] = Future()
    state.push_future = existing_future
    _push_focused_agent(state)
    assert state.push_future is existing_future


def test_push_focused_agent_no_focus_does_not_set_pushing() -> None:
    state = _make_state()
    _push_focused_agent(state)
    assert state.pushing_agent_name is None


def test_push_focused_agent_no_work_dir_shows_message() -> None:
    state = _make_state_with_focus(entries=(_make_entry(name="remote-agent", work_dir=None),))
    _push_focused_agent(state)
    assert "Cannot push" in state.footer_left_text.get_text()[0]


def test_push_focused_agent_with_work_dir_starts_push() -> None:
    """Push with a real work_dir creates an executor and submits a push future."""
    state = _make_state_with_focus(entries=(_make_entry(name="local-agent", work_dir=Path("/tmp/nonexistent")),))
    _push_focused_agent(state)
    assert state.pushing_agent_name == AgentName("local-agent")
    assert "Pushing local-agent" in state.footer_left_text.get_text()[0]
    assert state.push_future is not None
    assert state.executor is not None
    state.executor.shutdown(wait=False, cancel_futures=True)


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
# Tests for _on_delete_poll and _finish_delete
# =============================================================================


def test_on_delete_poll_no_future_does_not_schedule() -> None:
    state = _make_state()
    state.delete_future = None
    loop = _make_mock_loop()
    _on_delete_poll(loop, state)
    assert loop._alarm_tracker.call_count == 0


def test_on_delete_poll_with_done_future_clears_it() -> None:
    state = _make_state(snapshot=_make_snapshot(entries=(_make_entry(name="agent-a"),)))
    state.delete_future = _make_done_future(subprocess.CompletedProcess(args=[], returncode=0))
    state.deleting_agent_name = AgentName("agent-a")
    _on_delete_poll(_make_mock_loop(), state)
    assert state.delete_future is None


def test_on_delete_poll_not_done_schedules_next_tick() -> None:
    state = _make_state()
    state.delete_future = Future()
    state.deleting_agent_name = AgentName("agent-a")
    loop = _make_mock_loop()
    _on_delete_poll(loop, state)
    assert loop._alarm_tracker.call_count == 1


def test_finish_delete_success_shows_message() -> None:
    state = _make_state(snapshot=_make_snapshot(entries=(_make_entry(name="agent-a"),)))
    state.delete_future = _make_done_future(subprocess.CompletedProcess(args=[], returncode=0))
    state.deleting_agent_name = AgentName("agent-a")
    _finish_delete(_make_mock_loop(), state)
    assert state.delete_future is None
    assert state.deleting_agent_name is None
    assert "Deleted agent-a" in state.footer_left_text.get_text()[0]


def test_finish_delete_failure_shows_error() -> None:
    state = _make_state(snapshot=_make_snapshot(entries=(_make_entry(name="agent-a"),)))
    state.delete_future = _make_done_future(subprocess.CompletedProcess(args=[], returncode=1, stderr="some error"))
    state.deleting_agent_name = AgentName("agent-a")
    _finish_delete(_make_mock_loop(), state)
    assert "Failed to delete" in state.footer_left_text.get_text()[0]


def test_finish_delete_exception_shows_error() -> None:
    state = _make_state(snapshot=_make_snapshot(entries=(_make_entry(name="agent-a"),)))
    state.delete_future = _make_failed_future(RuntimeError("connection lost"))
    state.deleting_agent_name = AgentName("agent-a")
    _finish_delete(_make_mock_loop(), state)
    assert "Failed to delete" in state.footer_left_text.get_text()[0]


def test_finish_delete_no_future_does_not_change_state() -> None:
    state = _make_state()
    state.delete_future = None
    original_text = state.footer_left_text.get_text()[0]
    _finish_delete(_make_mock_loop(), state)
    assert state.footer_left_text.get_text()[0] == original_text


# =============================================================================
# Tests for _on_push_poll and _finish_push
# =============================================================================


def test_on_push_poll_no_future_does_not_schedule() -> None:
    state = _make_state()
    state.push_future = None
    loop = _make_mock_loop()
    _on_push_poll(loop, state)
    assert loop._alarm_tracker.call_count == 0


def test_on_push_poll_done_future_clears_it() -> None:
    state = _make_state(snapshot=_make_snapshot(entries=(_make_entry(name="agent-a"),)))
    state.push_future = _make_done_future(subprocess.CompletedProcess(args=[], returncode=0))
    state.pushing_agent_name = AgentName("agent-a")
    _on_push_poll(_make_mock_loop(), state)
    assert state.push_future is None


def test_on_push_poll_not_done_schedules_next() -> None:
    state = _make_state()
    state.push_future = Future()
    state.pushing_agent_name = AgentName("agent-a")
    loop = _make_mock_loop()
    _on_push_poll(loop, state)
    assert loop._alarm_tracker.call_count == 1


def test_finish_push_success_shows_message() -> None:
    state = _make_state(snapshot=_make_snapshot(entries=(_make_entry(name="agent-a"),)))
    state.push_future = _make_done_future(subprocess.CompletedProcess(args=[], returncode=0))
    state.pushing_agent_name = AgentName("agent-a")
    _finish_push(_make_mock_loop(), state)
    assert state.push_future is None
    assert "Pushed agent-a" in state.footer_left_text.get_text()[0]


def test_finish_push_failure_shows_error() -> None:
    state = _make_state(snapshot=_make_snapshot(entries=(_make_entry(name="agent-a"),)))
    state.push_future = _make_done_future(subprocess.CompletedProcess(args=[], returncode=1, stderr="rejected"))
    state.pushing_agent_name = AgentName("agent-a")
    _finish_push(_make_mock_loop(), state)
    assert "Failed to push" in state.footer_left_text.get_text()[0]


def test_finish_push_exception_shows_error() -> None:
    state = _make_state(snapshot=_make_snapshot(entries=(_make_entry(name="agent-a"),)))
    state.push_future = _make_failed_future(RuntimeError("timeout"))
    state.pushing_agent_name = AgentName("agent-a")
    _finish_push(_make_mock_loop(), state)
    assert "Failed to push" in state.footer_left_text.get_text()[0]


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
    _dispatch_command(state, "x", cmd)
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
# Tests for _carry_forward_pr_data and _build_board_widgets PR failure handling
# (merged from main)
# =============================================================================


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


def test_carry_forward_pr_data_preserves_old_prs() -> None:
    pr = make_pr_info(number=42, head_branch="mng/agent-1")
    old_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=pr,
        create_pr_url=None,
    )
    old = BoardSnapshot(entries=(old_entry,), prs_loaded=True, fetch_time_seconds=1.0)

    new_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url=None,
    )
    new = BoardSnapshot(
        entries=(new_entry,),
        errors=("gh auth failed",),
        prs_loaded=False,
        fetch_time_seconds=2.0,
    )

    result = _carry_forward_pr_data(old, new)
    assert result.prs_loaded is True
    assert result.entries[0].pr is not None
    assert result.entries[0].pr.number == 42
    assert "gh auth failed" in result.errors[0]
    assert result.fetch_time_seconds == 2.0


def test_carry_forward_pr_data_preserves_create_pr_url_without_pr() -> None:
    old_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url="https://github.com/org/repo/compare/mng/agent-1?expand=1",
    )
    old = BoardSnapshot(entries=(old_entry,), prs_loaded=True, fetch_time_seconds=1.0)

    new_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url=None,
    )
    new = BoardSnapshot(entries=(new_entry,), prs_loaded=False, fetch_time_seconds=2.0)

    result = _carry_forward_pr_data(old, new)
    assert result.prs_loaded is True
    assert result.entries[0].pr is None
    assert result.entries[0].create_pr_url == "https://github.com/org/repo/compare/mng/agent-1?expand=1"


def test_carry_forward_pr_data_handles_new_agents() -> None:
    old = BoardSnapshot(entries=(), prs_loaded=True, fetch_time_seconds=1.0)

    new_entry = AgentBoardEntry(
        name=AgentName("agent-new"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-new",
    )
    new = BoardSnapshot(entries=(new_entry,), prs_loaded=False, fetch_time_seconds=2.0)

    result = _carry_forward_pr_data(old, new)
    assert result.entries[0].pr is None


def test_first_load_pr_failure_shows_prs_not_loaded() -> None:
    entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url=None,
    )
    snapshot = BoardSnapshot(
        entries=(entry,),
        errors=("gh pr list failed: auth required",),
        prs_loaded=False,
        fetch_time_seconds=1.0,
    )
    walker, _ = _build_board_widgets(snapshot)

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
        branch="mng/agent-1",
        pr=None,
        create_pr_url="https://github.com/org/repo/compare/mng/agent-1?expand=1",
    )
    snapshot = BoardSnapshot(
        entries=(entry,),
        prs_loaded=True,
        fetch_time_seconds=1.0,
    )
    walker, _ = _build_board_widgets(snapshot)

    texts = _extract_text(list(walker))
    assert _text_contains(texts, "no PR yet")
    assert not _text_contains(texts, "PRs not loaded")


def test_second_load_pr_failure_shows_carried_forward_prs() -> None:
    pr = make_pr_info(number=42, head_branch="mng/agent-1")
    old_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=pr,
        create_pr_url=None,
    )
    old = BoardSnapshot(entries=(old_entry,), prs_loaded=True, fetch_time_seconds=1.0)

    new_entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("modal"),
        branch="mng/agent-1",
        pr=None,
        create_pr_url=None,
    )
    new = BoardSnapshot(
        entries=(new_entry,),
        errors=("gh pr list failed: network error",),
        prs_loaded=False,
        fetch_time_seconds=2.0,
    )

    carried = _carry_forward_pr_data(old, new)
    walker, _ = _build_board_widgets(carried)

    texts = _extract_text(list(walker))
    assert _text_contains(texts, "github.com/org/repo/pull/42")
    assert not _text_contains(texts, "PRs not loaded")
    assert not _text_contains(texts, "no PR yet")
    assert not _text_contains(texts, "create PR")
    assert _text_contains(texts, "network error")
