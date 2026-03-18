from pathlib import Path

import pytest

from imbue.minds.cli.update import MindAgentRecord
from imbue.minds.cli.update import parse_mind_agent_record
from imbue.minds.errors import MindError
from imbue.mng.primitives import AgentId


def test_mind_agent_record_stores_fields() -> None:
    """Verify MindAgentRecord stores agent_id and work_dir."""
    agent_id = AgentId()
    record = MindAgentRecord(agent_id=agent_id, work_dir=Path("/tmp/test"))
    assert record.agent_id == agent_id
    assert record.work_dir == Path("/tmp/test")


def test_parse_mind_agent_record_extracts_fields() -> None:
    """Verify parse_mind_agent_record extracts id and work_dir from raw dict."""
    valid_id = "agent-" + "a" * 32
    raw = {"id": valid_id, "name": "selene", "work_dir": "/tmp/minds/selene"}
    record = parse_mind_agent_record(raw, "selene")
    assert str(record.agent_id) == valid_id
    assert record.work_dir == Path("/tmp/minds/selene")


def test_parse_mind_agent_record_raises_on_missing_id() -> None:
    """Verify parse_mind_agent_record raises MindError when id is missing."""
    raw: dict[str, object] = {"name": "selene", "work_dir": "/tmp/minds/selene"}
    with pytest.raises(MindError, match="missing required fields"):
        parse_mind_agent_record(raw, "selene")


def test_parse_mind_agent_record_raises_on_missing_work_dir() -> None:
    """Verify parse_mind_agent_record raises MindError when work_dir is missing."""
    valid_id = "agent-" + "a" * 32
    raw: dict[str, object] = {"id": valid_id, "name": "selene"}
    with pytest.raises(MindError, match="missing required fields"):
        parse_mind_agent_record(raw, "selene")
