"""Tests for the startup notice webchat plugin."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from imbue.mngr_llm.resources.webchat_plugins.webchat_startup_notice import StartupNoticePlugin
from imbue.mngr_llm.resources.webchat_plugins.webchat_startup_notice import is_session_started


def test_is_session_started_returns_true_when_file_exists(tmp_path: Path) -> None:
    (tmp_path / "session_started").touch()
    assert is_session_started(str(tmp_path)) is True


def test_is_session_started_returns_false_when_file_missing(tmp_path: Path) -> None:
    assert is_session_started(str(tmp_path)) is False


def test_is_session_started_returns_true_when_dir_empty_string() -> None:
    """When MNGR_AGENT_STATE_DIR is unset, assume started (no banner needed)."""
    assert is_session_started("") is True


def test_startup_notice_plugin_registers_route() -> None:
    app = FastAPI()
    plugin = StartupNoticePlugin(agent_state_dir="", agent_name="")
    plugin.endpoint(app=app)
    api_routes = [route for route in app.routes if isinstance(route, APIRoute)]
    route_paths = [route.path for route in api_routes]
    assert "/api/agent-startup-status" in route_paths


def test_startup_status_endpoint_returns_started_when_no_state_dir() -> None:
    """When agent_state_dir is empty, the endpoint reports started=true."""
    app = FastAPI()
    plugin = StartupNoticePlugin(agent_state_dir="", agent_name="test-agent")
    plugin.endpoint(app=app)
    client = TestClient(app)
    response = client.get("/api/agent-startup-status")
    assert response.status_code == 200
    data = response.json()
    assert data["started"] is True
    assert data["agent_name"] == "test-agent"


def test_startup_status_endpoint_returns_not_started(tmp_path: Path) -> None:
    """The endpoint returns started=false when session_started file is missing."""
    app = FastAPI()
    plugin = StartupNoticePlugin(agent_state_dir=str(tmp_path), agent_name="my-mind")
    plugin.endpoint(app=app)
    client = TestClient(app)
    response = client.get("/api/agent-startup-status")
    assert response.status_code == 200
    data = response.json()
    assert data["started"] is False
    assert data["agent_name"] == "my-mind"


def test_startup_status_endpoint_returns_started_after_file_created(tmp_path: Path) -> None:
    """The endpoint reflects the session_started file appearing at runtime."""
    app = FastAPI()
    plugin = StartupNoticePlugin(agent_state_dir=str(tmp_path), agent_name="my-mind")
    plugin.endpoint(app=app)
    client = TestClient(app)

    response = client.get("/api/agent-startup-status")
    assert response.json()["started"] is False

    (tmp_path / "session_started").touch()

    response = client.get("/api/agent-startup-status")
    assert response.json()["started"] is True
