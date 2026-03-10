import json

import click
import pytest

from scripts.josh.workflow import find_agent_state_in_jsonl
from scripts.josh.workflow import parse_agent_identifier


def test_parse_agent_identifier_from_json_stdout() -> None:
    stdout = json.dumps({"agent_id": "agent-abc123", "host_id": "host-xyz"})
    result = parse_agent_identifier(stdout, "")
    assert result == "agent-abc123"


def test_parse_agent_identifier_from_jsonl_stdout() -> None:
    stdout = (
        json.dumps({"event": "info", "message": "starting"})
        + "\n"
        + json.dumps({"event": "created", "agent_id": "agent-def456", "host_id": "host-xyz"})
    )
    result = parse_agent_identifier(stdout, "")
    assert result == "agent-def456"


def test_parse_agent_identifier_from_stderr_fallback() -> None:
    stderr = (
        "2026-01-15 10:00:00.123 | INFO | Creating agent...\n"
        "2026-01-15 10:00:00.124 | INFO | Agent name: delightful-phoenix\n"
    )
    result = parse_agent_identifier("", stderr)
    assert result == "delightful-phoenix"


def test_parse_agent_identifier_prefers_json_over_stderr() -> None:
    stdout = json.dumps({"agent_id": "agent-from-json", "host_id": "host-xyz"})
    stderr = "2026-01-15 10:00:00 | INFO | Agent name: name-from-stderr\n"
    result = parse_agent_identifier(stdout, stderr)
    assert result == "agent-from-json"


def test_parse_agent_identifier_raises_on_no_match() -> None:
    with pytest.raises(click.ClickException, match="Could not parse agent identifier"):
        parse_agent_identifier("some random output", "some random stderr")


def test_parse_agent_identifier_raises_on_empty_output() -> None:
    with pytest.raises(click.ClickException, match="Could not parse agent identifier"):
        parse_agent_identifier("", "")


def test_parse_agent_identifier_skips_json_without_agent_id() -> None:
    stdout = json.dumps({"event": "info", "message": "starting"})
    stderr = "2026-01-15 10:00:00 | INFO | Agent name: fallback-agent\n"
    result = parse_agent_identifier(stdout, stderr)
    assert result == "fallback-agent"


def test_parse_agent_identifier_handles_malformed_json_lines() -> None:
    stdout = "not json\n" + json.dumps({"agent_id": "agent-good", "host_id": "host-1"})
    result = parse_agent_identifier(stdout, "")
    assert result == "agent-good"


def test_find_agent_state_in_jsonl_matches_by_name() -> None:
    jsonl = json.dumps({"name": "my-agent", "id": "agent-123", "state": "RUNNING"})
    result = find_agent_state_in_jsonl(jsonl, "my-agent")
    assert result == "RUNNING"


def test_find_agent_state_in_jsonl_matches_by_id() -> None:
    jsonl = json.dumps({"name": "my-agent", "id": "agent-123", "state": "WAITING"})
    result = find_agent_state_in_jsonl(jsonl, "agent-123")
    assert result == "WAITING"


def test_find_agent_state_in_jsonl_returns_none_when_not_found() -> None:
    jsonl = json.dumps({"name": "other-agent", "id": "agent-456", "state": "RUNNING"})
    result = find_agent_state_in_jsonl(jsonl, "my-agent")
    assert result is None


def test_find_agent_state_in_jsonl_returns_none_for_empty_output() -> None:
    result = find_agent_state_in_jsonl("", "my-agent")
    assert result is None


def test_find_agent_state_in_jsonl_handles_multiple_agents() -> None:
    jsonl = (
        json.dumps({"name": "agent-a", "id": "id-a", "state": "RUNNING"})
        + "\n"
        + json.dumps({"name": "agent-b", "id": "id-b", "state": "WAITING"})
        + "\n"
        + json.dumps({"name": "agent-c", "id": "id-c", "state": "DONE"})
    )
    assert find_agent_state_in_jsonl(jsonl, "agent-b") == "WAITING"
    assert find_agent_state_in_jsonl(jsonl, "agent-c") == "DONE"
    assert find_agent_state_in_jsonl(jsonl, "agent-a") == "RUNNING"


def test_find_agent_state_in_jsonl_skips_malformed_lines() -> None:
    jsonl = "not json\n" + json.dumps({"name": "my-agent", "id": "agent-123", "state": "STOPPED"})
    result = find_agent_state_in_jsonl(jsonl, "my-agent")
    assert result == "STOPPED"


def test_find_agent_state_in_jsonl_skips_non_dict_lines() -> None:
    jsonl = json.dumps([1, 2, 3]) + "\n" + json.dumps({"name": "my-agent", "id": "id-1", "state": "REPLACED"})
    result = find_agent_state_in_jsonl(jsonl, "my-agent")
    assert result == "REPLACED"
