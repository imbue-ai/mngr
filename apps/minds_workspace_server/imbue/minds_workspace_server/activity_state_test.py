from typing import Any

from imbue.minds_workspace_server.activity_state import ActivityState
from imbue.minds_workspace_server.activity_state import activity_state_label
from imbue.minds_workspace_server.activity_state import derive_activity_state
from imbue.minds_workspace_server.activity_state import has_unmatched_tool_use


def _assistant_with_tool_calls(*tool_call_ids: str) -> dict[str, Any]:
    return {
        "type": "assistant_message",
        "tool_calls": [{"tool_call_id": tcid, "tool_name": "Bash"} for tcid in tool_call_ids],
    }


def _tool_result(tool_call_id: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_call_id": tool_call_id}


def test_has_unmatched_tool_use_empty() -> None:
    assert has_unmatched_tool_use([]) is False


def test_has_unmatched_tool_use_no_tool_calls() -> None:
    events: list[dict[str, Any]] = [
        {"type": "user_message", "content": "hi"},
        {"type": "assistant_message", "tool_calls": []},
    ]
    assert has_unmatched_tool_use(events) is False


def test_has_unmatched_tool_use_unmatched() -> None:
    events = [_assistant_with_tool_calls("call_a")]
    assert has_unmatched_tool_use(events) is True


def test_has_unmatched_tool_use_all_matched() -> None:
    events = [_assistant_with_tool_calls("call_a"), _tool_result("call_a")]
    assert has_unmatched_tool_use(events) is False


def test_has_unmatched_tool_use_partially_matched() -> None:
    events = [_assistant_with_tool_calls("call_a", "call_b"), _tool_result("call_a")]
    assert has_unmatched_tool_use(events) is True


def test_has_unmatched_tool_use_handles_out_of_order_match() -> None:
    """A tool_result that arrives before the matching tool_use (theoretical) still matches."""
    events = [_tool_result("call_a"), _assistant_with_tool_calls("call_a")]
    assert has_unmatched_tool_use(events) is False


def test_has_unmatched_tool_use_skips_blocks_without_id() -> None:
    events: list[dict[str, Any]] = [
        {"type": "assistant_message", "tool_calls": [{"tool_name": "Bash"}]},
    ]
    assert has_unmatched_tool_use(events) is False


def test_derive_permissions_waiting_takes_priority() -> None:
    state = derive_activity_state(
        active_marker_present=True,
        permissions_waiting_marker_present=True,
        has_pending_tool_use=True,
    )
    assert state == ActivityState.WAITING_ON_PERMISSION


def test_derive_permissions_waiting_takes_priority_even_when_inactive() -> None:
    state = derive_activity_state(
        active_marker_present=False,
        permissions_waiting_marker_present=True,
        has_pending_tool_use=False,
    )
    assert state == ActivityState.WAITING_ON_PERMISSION


def test_derive_idle_when_no_active_marker() -> None:
    state = derive_activity_state(
        active_marker_present=False,
        permissions_waiting_marker_present=False,
        has_pending_tool_use=False,
    )
    assert state == ActivityState.IDLE


def test_derive_thinking_when_active_no_pending_tool() -> None:
    state = derive_activity_state(
        active_marker_present=True,
        permissions_waiting_marker_present=False,
        has_pending_tool_use=False,
    )
    assert state == ActivityState.THINKING


def test_derive_tool_running_when_active_and_pending_tool() -> None:
    state = derive_activity_state(
        active_marker_present=True,
        permissions_waiting_marker_present=False,
        has_pending_tool_use=True,
    )
    assert state == ActivityState.TOOL_RUNNING


def test_activity_state_labels() -> None:
    assert activity_state_label(ActivityState.IDLE) is None
    assert activity_state_label(ActivityState.THINKING) == "Thinking…"
    assert activity_state_label(ActivityState.TOOL_RUNNING) == "Running tool…"
    assert activity_state_label(ActivityState.WAITING_ON_PERMISSION) == "Waiting for permission"
