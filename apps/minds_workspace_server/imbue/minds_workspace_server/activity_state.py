"""Per-agent activity state surfaced on the chat panel.

The state is derived from two inputs:
- the marker files written by the Claude readiness hooks
  (``$MNGR_AGENT_STATE_DIR/active`` and ``permissions_waiting``)
- the parsed transcript events from the agent's session JSONL files
  (used only to distinguish ``THINKING`` from ``TOOL_RUNNING``)

Marker semantics live in ``mngr_claude.claude_config.build_readiness_hooks_config``.
"""

from collections.abc import Sequence
from enum import auto
from typing import Any

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.pure import pure


class ActivityState(UpperCaseStrEnum):
    """The activity state of a chat agent, as surfaced above the message input."""

    IDLE = auto()
    THINKING = auto()
    TOOL_RUNNING = auto()
    WAITING_ON_PERMISSION = auto()


def has_unmatched_tool_use(events: Sequence[dict[str, Any]]) -> bool:
    """True iff the transcript has at least one ``tool_use`` without a matching ``tool_result``.

    Walks every event so that an unmatched ``tool_use`` from any prior assistant
    turn still counts -- in practice Claude only ever has outstanding tool calls
    from its most recent assistant message, but the matching is order-independent
    so we don't have to care.
    """
    pending: set[str] = set()
    matched: set[str] = set()
    for event in events:
        event_type = event.get("type")
        if event_type == "assistant_message":
            for tool_call in event.get("tool_calls") or ():
                tool_call_id = tool_call.get("tool_call_id")
                if tool_call_id:
                    pending.add(tool_call_id)
        elif event_type == "tool_result":
            tool_call_id = event.get("tool_call_id")
            if tool_call_id:
                matched.add(tool_call_id)
        else:
            # user_message or other event types we don't care about for tool tracking.
            pass
    return bool(pending - matched)


@pure
def derive_activity_state(
    *,
    active_marker_present: bool,
    permissions_waiting_marker_present: bool,
    has_pending_tool_use: bool,
) -> ActivityState:
    """Combine marker-file presence and pending-tool state into an ``ActivityState``.

    Priority: ``permissions_waiting`` > ``active`` > tool-pending.
    """
    if permissions_waiting_marker_present:
        return ActivityState.WAITING_ON_PERMISSION
    if not active_marker_present:
        return ActivityState.IDLE
    if has_pending_tool_use:
        return ActivityState.TOOL_RUNNING
    return ActivityState.THINKING
