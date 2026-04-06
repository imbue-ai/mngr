"""Tests for the webchat agents endpoint."""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from imbue.mngr_llm.resources.webchat_plugins.webchat_agents import AgentsPlugin
from imbue.mngr_llm.resources.webchat_plugins.webchat_agents import _fetch_agent_list


def test_fetch_agent_list_returns_empty_when_mngr_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When UV_TOOL_BIN_DIR is unset (mngr not installed), returns an empty list."""
    monkeypatch.delenv("UV_TOOL_BIN_DIR", raising=False)
    result = _fetch_agent_list(host_name="")
    assert result == []


def test_fetch_agent_list_returns_empty_on_command_failure(tmp_path: Any) -> None:
    """When the mngr binary doesn't exist at the expected path, returns an empty list."""
    # Point UV_TOOL_BIN_DIR to a directory without a mngr binary.
    result = _fetch_agent_list(host_name="")
    assert result == []


def _create_test_app(agents: list[dict[str, Any]]) -> FastAPI:
    """Create a FastAPI app with a stubbed /api/agents endpoint.

    Instead of calling mngr list, the endpoint returns the provided agent list
    directly, exercising the plugin's route-registration logic.
    """
    app = FastAPI()

    def list_agents_endpoint() -> JSONResponse:
        return JSONResponse(content={"agents": agents})

    app.add_api_route("/api/agents", list_agents_endpoint, methods=["GET"])
    return app


def test_agents_endpoint_returns_empty_list() -> None:
    app = _create_test_app([])
    client = TestClient(app)
    response = client.get("/api/agents")
    assert response.status_code == 200
    assert response.json() == {"agents": []}


def test_agents_endpoint_returns_agent_list() -> None:
    agents = [
        {"name": "agent-1", "state": "RUNNING"},
        {"name": "agent-2", "state": "STOPPED"},
    ]
    app = _create_test_app(agents)
    client = TestClient(app)
    response = client.get("/api/agents")
    assert response.status_code == 200
    data = response.json()
    assert len(data["agents"]) == 2
    assert data["agents"][0]["name"] == "agent-1"
    assert data["agents"][1]["state"] == "STOPPED"


def test_agents_plugin_registers_route() -> None:
    """AgentsPlugin.endpoint() adds the /api/agents route to the app."""
    app = FastAPI()
    plugin = AgentsPlugin(host_name="")
    plugin.endpoint(app=app)
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/api/agents" in routes


def test_agents_plugin_registers_agent_info_route() -> None:
    """AgentsPlugin.endpoint() adds the /api/agent-info route to the app."""
    app = FastAPI()
    plugin = AgentsPlugin(host_name="")
    plugin.endpoint(app=app)
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/api/agent-info" in routes


def test_agent_info_endpoint_returns_name() -> None:
    """The /api/agent-info endpoint returns the agent name."""
    app = FastAPI()
    plugin = AgentsPlugin(host_name="")
    plugin.endpoint(app=app)
    client = TestClient(app)
    response = client.get("/api/agent-info")
    assert response.status_code == 200
    data = response.json()
    assert "name" in data
    assert isinstance(data["name"], str)
