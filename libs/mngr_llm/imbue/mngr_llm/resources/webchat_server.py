"""Webchat server based on llm-webchat.

Thin wrapper around llm-webchat's ``create_application`` that allows us to
configure it via environment variables and extend it with custom endpoints
(e.g. the Agents page).
"""

from __future__ import annotations

import importlib.resources
import os

import uvicorn
from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mng_llm import resources as llm_resources
from imbue.mng_llm.resources.webchat_agents import AgentsPlugin
from llm_webchat.config import Config
from llm_webchat.config import load_config
from llm_webchat.plugins import get_plugin_manager
from llm_webchat.server import create_application

_HOST_NAME = os.environ.get("MNG_HOST_NAME", "")


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


def _setup_agents_plugin() -> None:
    """Create and register the agents plugin with the llm-webchat plugin manager."""
    agents_plugin = AgentsPlugin(host_name=_HOST_NAME)
    get_plugin_manager().register(agents_plugin)


def _inject_plugin_static_files() -> None:
    """Register JS plugins and static files (CSS) with llm-webchat.

    Must be called before ``_build_config()`` since the config reads
    these env vars at construction time.
    """
    agents_js = _resolve_resource_path("webchat_agents.js")
    agents_css = _resolve_resource_path("webchat_agents.css")
    _prepend_to_env_list("LLM_WEBCHAT_JAVASCRIPT_PLUGINS", [agents_js])
    _prepend_to_env_list("LLM_WEBCHAT_STATIC_PATHS", [agents_css])


def main() -> None:
    """Entry point for the llmweb CLI command."""
    with log_span("Starting webchat server (llm-webchat)"):
        _setup_agents_plugin()
        _inject_plugin_static_files()

        config = _build_config()
        application = create_application(config)

        logger.info(
            "Webchat server listening on {}:{}",
            config.llm_webchat_host,
            config.llm_webchat_port,
        )
        uvicorn.run(
            application,
            host=config.llm_webchat_host,
            port=config.llm_webchat_port,
        )
