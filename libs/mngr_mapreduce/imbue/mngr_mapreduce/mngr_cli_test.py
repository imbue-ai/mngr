"""Unit tests for the mngr CLI subprocess wrapper."""

import json

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr_mapreduce.mngr_cli import CliError
from imbue.mngr_mapreduce.mngr_cli import _parse_list_json


def test_cli_error_is_mngr_error() -> None:
    # CliError must remain an MngrError so the reintegrate flow's
    # `except (CliError, OSError)` / MngrError handlers catch list failures.
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
