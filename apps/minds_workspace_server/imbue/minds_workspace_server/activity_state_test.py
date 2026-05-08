from typing import Any

from imbue.minds_workspace_server.activity_state import ActivityState
from imbue.minds_workspace_server.activity_state import derive_activity_state
from imbue.minds_workspace_server.activity_state import has_unmatched_tool_use
from imbue.minds_workspace_server.activity_state import last_event_type


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


def test_last_event_type_empty() -> None:
    assert last_event_type([]) is None


def test_last_event_type_returns_final() -> None:
    events: list[dict[str, Any]] = [
        {"type": "user_message"},
        {"type": "assistant_message", "tool_calls": []},
    ]
    assert last_event_type(events) == "assistant_message"


def test_last_event_type_missing_type_key() -> None:
    events: list[dict[str, Any]] = [{"foo": "bar"}]
    assert last_event_type(events) is None


def test_derive_permissions_waiting_takes_priority_over_pending_tool() -> None:
    state = derive_activity_state(
        permissions_waiting=True,
        has_pending_tool_use=True,
        tail_event_type="user_message",
    )
    assert state == ActivityState.WAITING_ON_PERMISSION


def test_derive_permissions_waiting_takes_priority_when_idle_signals() -> None:
    state = derive_activity_state(
        permissions_waiting=True,
        has_pending_tool_use=False,
        tail_event_type="assistant_message",
    )
    assert state == ActivityState.WAITING_ON_PERMISSION


def test_derive_tool_running_when_unmatched_tool_use() -> None:
    state = derive_activity_state(
        permissions_waiting=False,
        has_pending_tool_use=True,
        tail_event_type="assistant_message",
    )
    assert state == ActivityState.TOOL_RUNNING


def test_derive_thinking_when_last_event_is_user_message() -> None:
    state = derive_activity_state(
        permissions_waiting=False,
        has_pending_tool_use=False,
        tail_event_type="user_message",
    )
    assert state == ActivityState.THINKING


def test_derive_thinking_when_last_event_is_tool_result() -> None:
    state = derive_activity_state(
        permissions_waiting=False,
        has_pending_tool_use=False,
        tail_event_type="tool_result",
    )
    assert state == ActivityState.THINKING


def test_derive_idle_when_last_event_is_assistant_message() -> None:
    state = derive_activity_state(
        permissions_waiting=False,
        has_pending_tool_use=False,
        tail_event_type="assistant_message",
    )
    assert state == ActivityState.IDLE


def test_derive_idle_when_no_events() -> None:
    state = derive_activity_state(
        permissions_waiting=False,
        has_pending_tool_use=False,
        tail_event_type=None,
    )
    assert state == ActivityState.IDLE
