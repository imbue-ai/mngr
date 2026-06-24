"""Unit tests for the mngr CLI subprocess wrapper."""

import json

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.errors import MngrError
from imbue.mngr_mapreduce.mngr_cli import CliError
from imbue.mngr_mapreduce.mngr_cli import _parse_list_json
from imbue.mngr_mapreduce.mngr_cli import _run_mngr_raw


# The subprocess gets a 25s budget (was 10s, which left no headroom for a cold
# `mngr` start under heavy parallel load and intermittently raised CliError before
# the work actually finished); the 30s pytest-timeout stays the hard backstop above
# it. Still flaky-marked as a belt-and-suspenders retry. This asserts the wrapper
# returns a finished process, not that `mngr config` completes within any tight bound.
@pytest.mark.timeout(30)
@pytest.mark.flaky
def test_run_mngr_raw_returns_finished_process(cg: ConcurrencyGroup) -> None:
    result = _run_mngr_raw(["config", "list"], cg, timeout=25.0)
    assert result.returncode == 0


def test_cli_error_is_mngr_error() -> None:
    err = CliError("test failure")
    assert isinstance(err, MngrError)


def test_parse_list_json_empty_agents() -> None:
    result = _parse_list_json('{"agents": [], "errors": []}')
    assert result.agents == []


def test_parse_list_json_missing_agents_key() -> None:
    """When the top-level dict has no `agents` key, parse_list_json returns an empty ListResult."""
    result = _parse_list_json("{}")
    assert result.agents == []


def test_parse_list_json_invalid_json_raises() -> None:
    with pytest.raises(CliError):
        _parse_list_json("not json")


def test_parse_list_json_truncated_json_raises() -> None:
    with pytest.raises(CliError):
        _parse_list_json('{"agents": [')


def test_parse_list_json_bad_schema_raises() -> None:
    """A JSON object with `agents` whose entries lack required AgentDetails fields raises."""
    payload = json.dumps({"agents": [{"name": "missing required fields"}]})
    with pytest.raises(CliError):
        _parse_list_json(payload)
