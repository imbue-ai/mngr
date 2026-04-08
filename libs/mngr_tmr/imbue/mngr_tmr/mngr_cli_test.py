"""Unit tests for mngr_cli subprocess wrapper."""

import json

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.errors import MngrError
from imbue.mngr_tmr.mngr_cli import CliError
from imbue.mngr_tmr.mngr_cli import _parse_list_json
from imbue.mngr_tmr.mngr_cli import _run_mngr


def test_run_mngr_returns_stdout(cg: ConcurrencyGroup) -> None:
    result = _run_mngr(["config", "list"], cg, timeout=10.0)
    assert len(result) > 0


def test_run_mngr_raises_cli_error_on_failure(cg: ConcurrencyGroup) -> None:
    with pytest.raises(CliError):
        _run_mngr(["nonexistent-subcommand-xyz"], cg, timeout=10.0)


def test_cli_error_is_mngr_error() -> None:
    err = CliError("test failure")
    assert isinstance(err, MngrError)


def test_parse_list_json_empty_agents() -> None:
    result = _parse_list_json('{"agents": [], "errors": []}')
    assert result.agents == []


def test_parse_list_json_with_agents() -> None:
    agent_data = {
        "resource_type": "agent",
        "id": "agent-00000000000000000000000000000001",
        "name": "test-agent",
        "type": "claude",
        "command": "echo hello",
        "work_dir": "/tmp/work",
        "initial_branch": "mngr/test",
        "create_time": "2026-01-01T00:00:00Z",
        "start_on_boot": False,
        "state": "RUNNING",
        "labels": {"project": "test"},
        "host": {
            "id": "host-00000000000000000000000000000001",
            "name": "localhost",
            "provider_name": "local",
            "state": "RUNNING",
            "image": None,
            "tags": {},
            "boot_time": "2026-01-01T00:00:00Z",
            "uptime_seconds": 100.0,
            "resource": {"cpu": {"count": 4, "frequency_ghz": 3.0}, "memory_gb": 16.0, "disk_gb": 100.0, "gpu": None},
            "ssh": None,
            "snapshots": [],
            "is_locked": False,
            "locked_time": None,
            "plugin": {},
            "ssh_activity_time": None,
            "failure_reason": None,
        },
        "plugin": {},
    }
    raw = json.dumps({"agents": [agent_data], "errors": []})
    result = _parse_list_json(raw)
    assert len(result.agents) == 1
    assert str(result.agents[0].name) == "test-agent"
    assert result.agents[0].initial_branch == "mngr/test"
    assert result.agents[0].labels == {"project": "test"}


def test_parse_list_json_invalid_json() -> None:
    with pytest.raises(CliError, match="invalid JSON"):
        _parse_list_json("not json at all")


def test_parse_list_json_missing_agents_key() -> None:
    result = _parse_list_json('{"errors": []}')
    assert result.agents == []


def test_parse_list_json_invalid_schema() -> None:
    raw = json.dumps({"agents": [{"id": "not-a-valid-agent"}]})
    with pytest.raises(CliError, match="unexpected schema"):
        _parse_list_json(raw)
