import json
from pathlib import Path

from imbue.mngr_forward.snapshot import _parse_snapshot
from imbue.mngr_forward.testing import TEST_AGENT_ID_1
from imbue.mngr_forward.testing import TEST_AGENT_ID_2


def test_parse_empty_snapshot_returns_no_agents() -> None:
    result = _parse_snapshot("")
    assert result.agents == ()


def test_parse_snapshot_with_local_agent() -> None:
    payload = json.dumps(
        {
            "agents": [
                {
                    "id": str(TEST_AGENT_ID_1),
                    "labels": {"workspace": "true"},
                }
            ]
        }
    )
    result = _parse_snapshot(payload)
    assert len(result.agents) == 1
    entry = result.agents[0]
    assert entry.agent_id == TEST_AGENT_ID_1
    assert entry.ssh_info is None
    assert entry.labels == {"workspace": "true"}


def test_parse_snapshot_with_remote_agent() -> None:
    payload = json.dumps(
        {
            "agents": [
                {
                    "id": str(TEST_AGENT_ID_1),
                    "host": {
                        "ssh": {
                            "user": "root",
                            "host": "1.2.3.4",
                            "port": 22,
                            "key_path": "/tmp/k",
                        }
                    },
                    "labels": {},
                }
            ]
        }
    )
    result = _parse_snapshot(payload)
    [entry] = result.agents
    assert entry.ssh_info is not None
    assert entry.ssh_info.host == "1.2.3.4"
    assert entry.ssh_info.port == 22
    assert entry.ssh_info.key_path == Path("/tmp/k")


def test_parse_snapshot_skips_agents_without_id() -> None:
    payload = json.dumps({"agents": [{"labels": {}}, {"id": str(TEST_AGENT_ID_2)}]})
    result = _parse_snapshot(payload)
    assert len(result.agents) == 1
    assert result.agents[0].agent_id == TEST_AGENT_ID_2


def test_parse_snapshot_extracts_name_host_id_provider_name() -> None:
    payload = json.dumps(
        {
            "agents": [
                {
                    "id": str(TEST_AGENT_ID_1),
                    "name": "my-agent",
                    "host": {
                        "id": "host-1",
                        "provider_name": "modal",
                    },
                    "labels": {},
                }
            ]
        }
    )
    result = _parse_snapshot(payload)
    [entry] = result.agents
    assert entry.agent_name == "my-agent"
    assert entry.host_id == "host-1"
    assert entry.provider_name == "modal"


def test_parse_snapshot_defaults_missing_filter_fields_to_empty_string() -> None:
    """An older mngr list payload that lacks the filter fields still parses."""
    payload = json.dumps(
        {
            "agents": [
                {
                    "id": str(TEST_AGENT_ID_1),
                    "labels": {},
                }
            ]
        }
    )
    result = _parse_snapshot(payload)
    [entry] = result.agents
    assert entry.agent_name == ""
    assert entry.host_id == ""
    assert entry.provider_name == ""
