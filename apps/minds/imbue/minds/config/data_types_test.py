import json
from pathlib import Path

from imbue.minds.config.data_types import MindPaths
from imbue.minds.config.data_types import get_default_data_dir
from imbue.minds.config.data_types import parse_agents_from_mngr_output
from imbue.mngr.primitives import AgentId


def test_mind_paths_mind_dir_uses_agent_id(tmp_path: Path) -> None:
    """Verify mind_dir incorporates the agent_id into the path."""
    paths = MindPaths(data_dir=tmp_path)
    agent_id = AgentId()

    result = paths.mind_dir(agent_id)
    assert result.parent == tmp_path
    assert str(agent_id) in str(result)


def test_mind_paths_auth_dir_is_under_data_dir(tmp_path: Path) -> None:
    paths = MindPaths(data_dir=tmp_path)
    assert paths.auth_dir == tmp_path / "auth"


def test_get_default_data_dir_returns_home_minds() -> None:
    result = get_default_data_dir()
    assert result.name == ".minds"
    assert result.parent == Path.home()


# -- parse_agents_from_mngr_output tests --


def test_parse_agents_from_mngr_output_extracts_records() -> None:
    """Verify parse_agents_from_mngr_output extracts agent records from JSON."""
    json_str = json.dumps(
        {
            "agents": [
                {"id": "agent-abc123", "name": "selene", "work_dir": "/tmp/minds/selene"},
            ]
        }
    )
    agents = parse_agents_from_mngr_output(json_str)
    assert len(agents) == 1
    assert agents[0]["id"] == "agent-abc123"
    assert agents[0]["name"] == "selene"


def test_parse_agents_from_mngr_output_handles_empty() -> None:
    """Verify parse_agents_from_mngr_output returns empty list for no agents."""
    json_str = json.dumps({"agents": []})
    agents = parse_agents_from_mngr_output(json_str)
    assert agents == []


def test_parse_agents_from_mngr_output_handles_non_json() -> None:
    """Verify parse_agents_from_mngr_output handles non-JSON output gracefully."""
    agents = parse_agents_from_mngr_output("not json at all")
    assert agents == []


def test_parse_agents_from_mngr_output_handles_mixed_output() -> None:
    """Verify parse_agents_from_mngr_output handles SSH errors mixed with JSON."""
    output = "WARNING: some SSH error\n" + json.dumps({"agents": [{"id": "agent-xyz", "name": "test"}]})
    agents = parse_agents_from_mngr_output(output)
    assert len(agents) == 1
    assert agents[0]["id"] == "agent-xyz"
