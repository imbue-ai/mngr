from typing import Any

import pytest

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


@pytest.mark.parametrize(
    "events, expected",
    [
        pytest.param([], False, id="empty_transcript"),
        pytest.param(
            [
                {"type": "user_message", "content": "hi"},
                {"type": "assistant_message", "tool_calls": []},
            ],
            False,
            id="no_tool_calls",
        ),
        pytest.param([_assistant_with_tool_calls("call_a")], True, id="single_unmatched"),
        pytest.param(
            [_assistant_with_tool_calls("call_a"), _tool_result("call_a")],
            False,
            id="all_matched",
        ),
        pytest.param(
            [_assistant_with_tool_calls("call_a", "call_b"), _tool_result("call_a")],
            True,
            id="partially_matched",
        ),
        # A tool_result that arrives before the matching tool_use (theoretical) still matches.
        pytest.param(
            [_tool_result("call_a"), _assistant_with_tool_calls("call_a")],
            False,
            id="out_of_order_match",
        ),
        pytest.param(
            [{"type": "assistant_message", "tool_calls": [{"tool_name": "Bash"}]}],
            False,
            id="skips_blocks_without_id",
        ),
    ],
)
def test_has_unmatched_tool_use(events: list[dict[str, Any]], expected: bool) -> None:
    assert has_unmatched_tool_use(events) is expected


@pytest.mark.parametrize(
    "events, expected",
    [
        pytest.param([], None, id="empty_transcript"),
        pytest.param(
            [
                {"type": "user_message"},
                {"type": "assistant_message", "tool_calls": []},
            ],
            "assistant_message",
            id="returns_final",
        ),
        pytest.param([{"foo": "bar"}], None, id="missing_type_key"),
    ],
)
def test_last_event_type(events: list[dict[str, Any]], expected: str | None) -> None:
    assert last_event_type(events) == expected


@pytest.mark.parametrize(
    "permissions_waiting, has_pending_tool_use, tail_event_type, expected",
    [
        pytest.param(
            True,
            True,
            "user_message",
            ActivityState.WAITING_ON_PERMISSION,
            id="permissions_overrides_pending_tool",
        ),
        pytest.param(
            True,
            False,
            "assistant_message",
            ActivityState.WAITING_ON_PERMISSION,
            id="permissions_overrides_idle_signals",
        ),
        pytest.param(
            False,
            True,
            "assistant_message",
            ActivityState.TOOL_RUNNING,
            id="tool_running_when_unmatched_tool_use",
        ),
        pytest.param(
            False,
            False,
            "user_message",
            ActivityState.THINKING,
            id="thinking_when_last_event_is_user_message",
        ),
        pytest.param(
            False,
            False,
            "tool_result",
            ActivityState.THINKING,
            id="thinking_when_last_event_is_tool_result",
        ),
        pytest.param(
            False,
            False,
            "assistant_message",
            ActivityState.IDLE,
            id="idle_when_last_event_is_assistant_message",
        ),
        pytest.param(
            False,
            False,
            None,
            ActivityState.IDLE,
            id="idle_when_no_events",
        ),
    ],
)
def test_derive_activity_state(
    permissions_waiting: bool,
    has_pending_tool_use: bool,
    tail_event_type: str | None,
    expected: ActivityState,
) -> None:
    state = derive_activity_state(
        permissions_waiting=permissions_waiting,
        has_pending_tool_use=has_pending_tool_use,
        tail_event_type=tail_event_type,
    )
    assert state == expected
