import json

from imbue.minds.cli.update import parse_agents_from_mng_output


def testparse_agents_from_mng_output_extracts_records() -> None:
    """Verify parse_agents_from_mng_output extracts agent records from JSON."""
    json_str = json.dumps(
        {
            "agents": [
                {"id": "agent-abc123", "name": "selene", "work_dir": "/tmp/minds/selene"},
            ]
        }
    )
    agents = parse_agents_from_mng_output(json_str)
    assert len(agents) == 1
    assert agents[0]["id"] == "agent-abc123"
    assert agents[0]["name"] == "selene"


def testparse_agents_from_mng_output_handles_empty() -> None:
    """Verify parse_agents_from_mng_output returns empty list for no agents."""
    json_str = json.dumps({"agents": []})
    agents = parse_agents_from_mng_output(json_str)
    assert agents == []


def testparse_agents_from_mng_output_handles_non_json() -> None:
    """Verify parse_agents_from_mng_output handles non-JSON output gracefully."""
    agents = parse_agents_from_mng_output("not json at all")
    assert agents == []


def testparse_agents_from_mng_output_handles_mixed_output() -> None:
    """Verify parse_agents_from_mng_output handles SSH errors mixed with JSON."""
    output = "WARNING: some SSH error\n" + json.dumps({"agents": [{"id": "agent-xyz", "name": "test"}]})
    agents = parse_agents_from_mng_output(output)
    assert len(agents) == 1
    assert agents[0]["id"] == "agent-xyz"
