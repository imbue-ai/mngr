"""Tests for the ``mngr foreman create`` bootstrap agent-id parsing."""

from __future__ import annotations

from imbue.mngr_foreman.cli.create import _parse_created_agent_id


def test_parses_agent_id_from_json_line() -> None:
    out = '{"agent_id": "agent-abc123", "host_id": "host-x", "host_name": "h"}\n'
    assert _parse_created_agent_id(out) == "agent-abc123"


def test_picks_last_json_object_amid_noise() -> None:
    out = "\n".join(
        [
            "some log line",
            '{"agent_id": "agent-old", "host_id": "h1"}',
            "more logs",
            '{"agent_id": "agent-new", "host_id": "h2"}',
        ]
    )
    assert _parse_created_agent_id(out) == "agent-new"


def test_returns_none_without_agent_id() -> None:
    assert _parse_created_agent_id("no json here\njust text") is None
    assert _parse_created_agent_id('{"host_id": "h", "no_agent": true}') is None
    assert _parse_created_agent_id("") is None


def test_ignores_malformed_json_lines() -> None:
    out = '{not valid json\n{"agent_id": "agent-ok"}'
    assert _parse_created_agent_id(out) == "agent-ok"
