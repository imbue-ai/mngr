"""Webchat server based on llm-webchat.

Thin wrapper around llm-webchat's ``create_application`` that allows us to
configure it via environment variables and extend it with custom endpoints
(e.g. the Agents page).

Reads the ``WEB_SERVER_PORT`` env var for the port (defaults to ``0``,
i.e. OS-assigned random port). Registers itself in
``servers/events.jsonl`` under the ``web`` server name so the forwarding
server can discover it.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import socket
import types
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import uvicorn
from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr_llm.resources import webchat_plugins as llm_webchat_plugins
from imbue.mngr_llm.resources.webchat_plugins.webchat_agents import AgentsPlugin
from imbue.mngr_llm.resources.webchat_plugins.webchat_default_model import DefaultModelPlugin
from imbue.mngr_llm.resources.webchat_plugins.webchat_greeting import GreetingPlugin
from imbue.mngr_llm.resources.webchat_plugins.webchat_injected_messages import InjectedMessagesPlugin
from imbue.mngr_llm.resources.webchat_plugins.webchat_register_conversations import RegisterConversationsPlugin
from imbue.mngr_llm.resources.webchat_plugins.webchat_system_prompt import create_system_prompt_plugin
from llm_webchat.config import Config
from llm_webchat.config import load_config
from llm_webchat.plugins import get_plugin_manager
from llm_webchat.server import create_application

_HOST_NAME: Final[str] = os.environ.get("MNG_HOST_NAME", "")
_AGENT_STATE_DIR: Final[str] = os.environ.get("MNGR_AGENT_STATE_DIR", "")
_AGENT_ID: Final[str] = os.environ.get("MNGR_AGENT_ID", "")
_LLM_USER_PATH: Final[str] = os.environ.get("LLM_USER_PATH", "")
_AGENT_WORK_DIR: Final[str] = os.environ.get("MNGR_AGENT_WORK_DIR", "")
_WEB_SERVER_NAME: Final[str] = "web"


def _compute_root_path(agent_id: str, server_name: str) -> str:
    """Compute the ASGI root_path for the webchat server.

    When served behind the forwarding server, the webchat is accessed at
    ``/agents/{agent_id}/{server_name}/``. Setting ``root_path`` tells the
    ASGI application about this prefix so that framework-generated URLs
    (redirects, links, etc.) are correct.

    Returns an empty string when the agent ID is not available (e.g. during
    local development without the forwarding server).
    """
    if not agent_id:
        return ""
    return f"/agents/{agent_id}/{server_name}"


def _resolve_resource_path(filename: str) -> str:
    """Return the absolute filesystem path of a resource file in this package."""
    resource_files = importlib.resources.files(llm_webchat_plugins)
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


def _setup_agents_plugin() -> None:
    """Create and register the agents plugin with the llm-webchat plugin manager."""
    agents_plugin = AgentsPlugin(host_name=_HOST_NAME)
    # Wrap in SimpleNamespace because pluggy's registration iterates all
    # attributes via getattr, which crashes on Pydantic v2 model instances.
    wrapped = types.SimpleNamespace(endpoint=agents_plugin.endpoint)
    get_plugin_manager().register(wrapped)


def _setup_system_prompt_plugin() -> None:
    """Create and register the system prompt plugin with the llm-webchat plugin manager."""
    plugin = create_system_prompt_plugin(_AGENT_WORK_DIR)
    if plugin is None:
        return
    wrapped = types.SimpleNamespace(modify_llm_prompt_command=plugin.modify_llm_prompt_command)
    get_plugin_manager().register(wrapped)


def _setup_injected_messages_plugin() -> None:
    """Create and register the injected-messages watcher plugin."""
    if not _LLM_USER_PATH:
        logger.warning("LLM_USER_PATH not set, injected-message watcher will not start")
        return
    db_path = Path(_LLM_USER_PATH) / "logs.db"
    plugin = InjectedMessagesPlugin(db_path=db_path)
    wrapped = types.SimpleNamespace(register_event_broadcaster=plugin.register_event_broadcaster)
    get_plugin_manager().register(wrapped)


def _setup_register_conversations_plugin() -> None:
    """Create and register the register-conversations plugin with the llm-webchat plugin manager."""
    if not _LLM_USER_PATH:
        logger.warning("LLM_USER_PATH not set, conversation registration plugin will not start")
        return
    db_path = Path(_LLM_USER_PATH) / "logs.db"
    plugin = RegisterConversationsPlugin(db_path=db_path)
    wrapped = types.SimpleNamespace(endpoint=plugin.endpoint)
    get_plugin_manager().register(wrapped)


def _setup_default_model_plugin() -> None:
    """Create and register the default-model plugin with the llm-webchat plugin manager."""
    default_model_plugin = DefaultModelPlugin()
    wrapped = types.SimpleNamespace(endpoint=default_model_plugin.endpoint)
    get_plugin_manager().register(wrapped)


def _setup_greeting_plugin() -> None:
    """Create and register the greeting-conversation plugin with the llm-webchat plugin manager."""
    greeting_plugin = GreetingPlugin(
        agent_work_dir=_AGENT_WORK_DIR,
        llm_user_path=_LLM_USER_PATH,
    )
    wrapped = types.SimpleNamespace(endpoint=greeting_plugin.endpoint)
    get_plugin_manager().register(wrapped)


def _inject_plugin_static_files() -> None:
    """Register JS plugins and static files (CSS) with llm-webchat.

    Must be called before ``_build_config()`` since the config reads
    these env vars at construction time.
    """
    agents_js = _resolve_resource_path("webchat_agents.js")
    agents_css = _resolve_resource_path("webchat_agents.css")
    injected_messages_js = _resolve_resource_path("webchat_injected_messages.js")
    default_model_js = _resolve_resource_path("webchat_default_model.js")
    greeting_js = _resolve_resource_path("webchat_greeting.js")
    _prepend_to_env_list(
        "LLM_WEBCHAT_JAVASCRIPT_PLUGINS",
        [agents_js, injected_messages_js, default_model_js, greeting_js],
    )
    _prepend_to_env_list("LLM_WEBCHAT_STATIC_PATHS", [agents_css])


def _bridge_web_server_port_env_var() -> None:
    """Bridge WEB_SERVER_PORT to LLM_WEBCHAT_PORT for backward compatibility.

    Some callers set WEB_SERVER_PORT; llm-webchat reads LLM_WEBCHAT_PORT
    (defaulting to 8000). This bridges them so callers that set
    WEB_SERVER_PORT get the expected behavior.

    Must be called before ``load_config()``.
    """
    web_server_port = os.environ.get("WEB_SERVER_PORT")
    if web_server_port is not None and "LLM_WEBCHAT_PORT" not in os.environ:
        os.environ["LLM_WEBCHAT_PORT"] = web_server_port
    # When neither is set, default to 0 (random port)
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
        for socket_ in sock_server.sockets:
            address = socket_.getsockname()
            return int(address[1])
    logger.warning("Could not determine bound port from uvicorn server")
    return 0


def main() -> None:
    """Entry point for the llmweb CLI command."""
    with log_span("Starting webchat server (llm-webchat)"):
        _setup_agents_plugin()
        _setup_default_model_plugin()
        _setup_greeting_plugin()
        _setup_injected_messages_plugin()
        _setup_register_conversations_plugin()
        _setup_system_prompt_plugin()
        _inject_plugin_static_files()
        _bridge_web_server_port_env_var()

        config = _build_config()
        application = create_application(config)

        root_path = _compute_root_path(agent_id=_AGENT_ID, server_name=_WEB_SERVER_NAME)
        uvicorn_config = uvicorn.Config(
            application,
            host=config.llm_webchat_host,
            port=config.llm_webchat_port,
            root_path=root_path,
        )
        server = uvicorn.Server(uvicorn_config)

        # Override the startup callback to register the server with
        # the actual bound port (which may differ from the requested
        # port when port=0).
        original_startup = server.startup

        async def _startup_with_registration(sockets: list[socket.socket] | None = None) -> None:
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
