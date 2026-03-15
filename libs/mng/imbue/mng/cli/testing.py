import json
from pathlib import Path
from typing import Any

from imbue.mng.primitives import AgentId


def create_agent_with_events_dir(
    per_host_dir: Path,
    agent_name: str,
    events_source: str | None = None,
    agent_type: str = "generic",
) -> tuple[AgentId, Path]:
    """Create a minimal agent directory with an events subdirectory.

    Returns (agent_id, events_dir) where events_dir is ready for test files.
    If events_source is given, events_dir is per_host_dir/agents/<id>/events/<source>;
    otherwise it is per_host_dir/agents/<id>/events.
    """
    agent_id = AgentId.generate()
    agent_dir = per_host_dir / "agents" / str(agent_id)
    agent_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "id": str(agent_id),
        "name": agent_name,
        "type": agent_type,
        "command": "sleep 1",
        "work_dir": "/tmp/test",
        "create_time": "2026-01-01T00:00:00+00:00",
    }
    (agent_dir / "data.json").write_text(json.dumps(data))
    if events_source is not None:
        events_dir = agent_dir / "events" / events_source
    else:
        events_dir = agent_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    return agent_id, events_dir


def write_common_transcript_events(
    events_dir: Path,
    events: list[dict[str, Any]],
) -> None:
    """Write a list of event dicts as JSONL to events.jsonl in the given directory."""
    (events_dir / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")


SAMPLE_TRANSCRIPT_EVENTS: list[dict[str, Any]] = [
    {
        "timestamp": "2026-01-01T00:00:00Z",
        "type": "user_message",
        "event_id": "e1",
        "source": "claude/common_transcript",
        "role": "user",
        "content": "Hello",
    },
    {
        "timestamp": "2026-01-01T00:00:01Z",
        "type": "assistant_message",
        "event_id": "e2",
        "source": "claude/common_transcript",
        "role": "assistant",
        "text": "World",
        "tool_calls": [],
        "model": "test-model",
    },
    {
        "timestamp": "2026-01-01T00:00:02Z",
        "type": "tool_result",
        "event_id": "e3",
        "source": "claude/common_transcript",
        "tool_name": "Bash",
        "output": "ok",
        "is_error": False,
    },
]


def create_agent_with_sample_transcript(
    per_host_dir: Path,
    agent_name: str,
    events: list[dict[str, Any]] | None = None,
) -> tuple[AgentId, Path]:
    """Create an agent with a populated common_transcript events file.

    Uses SAMPLE_TRANSCRIPT_EVENTS (user, assistant, tool_result) if no
    events are provided. Returns (agent_id, events_dir).
    """
    agent_id, events_dir = create_agent_with_events_dir(
        per_host_dir,
        agent_name=agent_name,
        events_source="claude/common_transcript",
        agent_type="claude",
    )
    write_common_transcript_events(events_dir, events if events is not None else SAMPLE_TRANSCRIPT_EVENTS)
    return agent_id, events_dir
