"""Webchat server based on llm-webchat.

Thin wrapper around llm-webchat's ``create_application`` that allows us to
configure it via environment variables and extend it with custom endpoints
(e.g. the Agents page).

Reads the ``WEB_SERVER_PORT`` env var for port compatibility with the
legacy ``web_server.py`` (defaults to ``0``, i.e. OS-assigned random port).
Registers itself in ``servers/events.jsonl`` under the ``web`` server name
so the forwarding server can discover it.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import types
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import uvicorn
from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr_llm import resources as llm_resources
from imbue.mngr_llm.resources.webchat_agents import AgentsPlugin
from llm_webchat.config import Config
from llm_webchat.config import load_config
from llm_webchat.plugins import get_plugin_manager
from llm_webchat.server import create_application

_HOST_NAME: Final[str] = os.environ.get("MNG_HOST_NAME", "")
_AGENT_STATE_DIR: Final[str] = os.environ.get("MNGR_AGENT_STATE_DIR", "")
_WEB_SERVER_NAME: Final[str] = "web"


def _resolve_resource_path(filename: str) -> str:
    """Return the absolute filesystem path of a resource file in this package."""
    resource_files = importlib.resources.files(llm_resources)
    resource = resource_files.joinpath(filename)
    return str(resource)


def _prepend_to_env_list(env_var: str, paths: list[str]) -> None:
    """Prepend paths to a comma-separated env var.

    Must be called *before* ``load_config()`` since the config reads
    env vars at construction time.
    """
    existing = os.environ.get(env_var, "")
    joined = ",".join(paths)
    if existing:
        joined = joined + "," + existing
    os.environ[env_var] = joined


def _build_config() -> Config:
    """Build the llm-webchat Config from the environment.

    llm-webchat's Config is a pydantic-settings BaseSettings, so it reads
    LLM_WEBCHAT_* env vars automatically. This function is the single place
    to apply any programmatic overrides on top of the env-driven defaults.
    """
    return load_config()


def _wrap_pydantic_plugin_for_pluggy(plugin: AgentsPlugin) -> types.SimpleNamespace:
    """Wrap a Pydantic model plugin so pluggy can safely register it.

    Pluggy iterates over all attributes via getattr during registration.
    Pydantic v2 raises AttributeError for class-only descriptors like
    __signature__ on model instances, which crashes pluggy. This wrapper
    creates a plain SimpleNamespace that exposes only the hookimpl methods.
    """
    return types.SimpleNamespace(endpoint=plugin.endpoint)


def _setup_agents_plugin() -> None:
    """Create and register the agents plugin with the llm-webchat plugin manager."""
    agents_plugin = AgentsPlugin(host_name=_HOST_NAME)
    wrapped = _wrap_pydantic_plugin_for_pluggy(agents_plugin)
    get_plugin_manager().register(wrapped)


def _inject_plugin_static_files() -> None:
    """Register JS plugins and static files (CSS) with llm-webchat.

    Must be called before ``_build_config()`` since the config reads
    these env vars at construction time.
    """
    agents_js = _resolve_resource_path("webchat_agents.js")
    agents_css = _resolve_resource_path("webchat_agents.css")
    _prepend_to_env_list("LLM_WEBCHAT_JAVASCRIPT_PLUGINS", [agents_js])
    _prepend_to_env_list("LLM_WEBCHAT_STATIC_PATHS", [agents_css])


def _bridge_web_server_port_env_var() -> None:
    """Bridge WEB_SERVER_PORT to LLM_WEBCHAT_PORT for backward compatibility.

    The legacy web_server.py reads WEB_SERVER_PORT (defaulting to 0 for a
    random OS-assigned port). llm-webchat reads LLM_WEBCHAT_PORT (defaulting
    to 8000). This bridges them so callers that set WEB_SERVER_PORT get the
    expected behavior.

    Must be called before ``load_config()``.
    """
    web_server_port = os.environ.get("WEB_SERVER_PORT")
    if web_server_port is not None and "LLM_WEBCHAT_PORT" not in os.environ:
        os.environ["LLM_WEBCHAT_PORT"] = web_server_port
    # When neither is set, default to 0 (random port) to match legacy behavior
    if "LLM_WEBCHAT_PORT" not in os.environ:
        os.environ["LLM_WEBCHAT_PORT"] = "0"


def _iso_timestamp() -> str:
    """Return the current UTC time as an ISO 8601 timestamp with nanosecond precision."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond * 1000:09d}Z"


def _make_event_id(data: str) -> str:
    """Generate a deterministic event ID from content."""
    return "evt-" + hashlib.sha256(data.encode()).hexdigest()[:32]


def _register_server_to_events_jsonl(agent_state_dir: str, server_name: str, port: int) -> None:
    """Append a server record to servers/events.jsonl for forwarding server discovery."""
    if not agent_state_dir:
        return
    servers_jsonl_path = Path(agent_state_dir) / "events" / "servers" / "events.jsonl"
    servers_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{port}"
    record = json.dumps(
        {
            "timestamp": _iso_timestamp(),
            "type": "server_registered",
            "event_id": _make_event_id(f"{server_name}:{url}"),
            "source": "servers",
            "server": server_name,
            "url": url,
        }
    )
    with open(servers_jsonl_path, "a") as f:
        f.write(record + "\n")
    logger.debug("Registered server '{}' at {}", server_name, url)


def _get_bound_port(server: uvicorn.Server) -> int:
    """Extract the actual bound port from a started uvicorn Server.

    When port 0 is requested, the OS assigns a random port. This reads
    the real port from the underlying socket after the server has started.
    """
    for sock_server in server.servers:
        for socket in sock_server.sockets:
            address = socket.getsockname()
            return int(address[1])
    logger.warning("Could not determine bound port from uvicorn server")
    return 0


def main() -> None:
    """Entry point for the llmweb CLI command."""
    with log_span("Starting webchat server (llm-webchat)"):
        _setup_agents_plugin()
        _inject_plugin_static_files()
        _bridge_web_server_port_env_var()

        config = _build_config()
        application = create_application(config)

        uvicorn_config = uvicorn.Config(
            application,
            host=config.llm_webchat_host,
            port=config.llm_webchat_port,
        )
        server = uvicorn.Server(uvicorn_config)

        # Override the startup callback to register the server with
        # the actual bound port (which may differ from the requested
        # port when port=0).
        original_startup = server.startup

        async def _startup_with_registration() -> None:
            await original_startup()
            actual_port = _get_bound_port(server)
            logger.info(
                "Webchat server listening on {}:{}",
                config.llm_webchat_host,
                actual_port,
            )
            _register_server_to_events_jsonl(_AGENT_STATE_DIR, _WEB_SERVER_NAME, actual_port)

        server.startup = _startup_with_registration  # type: ignore[assignment]
        server.run()
