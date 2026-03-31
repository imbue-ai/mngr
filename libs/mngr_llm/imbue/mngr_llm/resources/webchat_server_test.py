"""Tests for the webchat server port bridging and server registration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from imbue.mngr_llm.resources.webchat_server import _bridge_web_server_port_env_var
from imbue.mngr_llm.resources.webchat_server import _compute_root_path
from imbue.mngr_llm.resources.webchat_server import _register_server_to_events_jsonl


def test_bridge_web_server_port_to_llm_webchat_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SERVER_PORT", "9999")
    monkeypatch.delenv("LLM_WEBCHAT_PORT", raising=False)
    _bridge_web_server_port_env_var()
    assert os.environ["LLM_WEBCHAT_PORT"] == "9999"


def test_bridge_does_not_override_explicit_llm_webchat_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SERVER_PORT", "9999")
    monkeypatch.setenv("LLM_WEBCHAT_PORT", "7777")
    _bridge_web_server_port_env_var()
    assert os.environ["LLM_WEBCHAT_PORT"] == "7777"


def test_bridge_defaults_to_zero_when_neither_port_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_SERVER_PORT", raising=False)
    monkeypatch.delenv("LLM_WEBCHAT_PORT", raising=False)
    _bridge_web_server_port_env_var()
    assert os.environ["LLM_WEBCHAT_PORT"] == "0"


def test_bridge_preserves_zero_from_web_server_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_SERVER_PORT", "0")
    monkeypatch.delenv("LLM_WEBCHAT_PORT", raising=False)
    _bridge_web_server_port_env_var()
    assert os.environ["LLM_WEBCHAT_PORT"] == "0"


def test_register_server_writes_record_to_events_jsonl(tmp_path: Path) -> None:
    _register_server_to_events_jsonl(
        agent_state_dir=str(tmp_path),
        server_name="web",
        port=12345,
    )
    events_file = tmp_path / "events" / "servers" / "events.jsonl"
    assert events_file.exists()
    record = json.loads(events_file.read_text().strip())
    assert record["type"] == "server_registered"
    assert record["server"] == "web"
    assert record["url"] == "http://127.0.0.1:12345"
    assert record["source"] == "servers"
    assert "event_id" in record
    assert "timestamp" in record


def test_compute_root_path_with_agent_id() -> None:
    assert _compute_root_path(agent_id="abc123", server_name="web") == "/agents/abc123/web"


def test_compute_root_path_without_agent_id() -> None:
    assert _compute_root_path(agent_id="", server_name="web") == ""


def test_register_server_does_nothing_when_agent_state_dir_is_empty() -> None:
    # Should not raise
    _register_server_to_events_jsonl(
        agent_state_dir="",
        server_name="web",
        port=12345,
    )
