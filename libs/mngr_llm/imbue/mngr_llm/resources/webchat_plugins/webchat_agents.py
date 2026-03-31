"""Agents and agent-info endpoints for the webchat server.

Provides a ``/api/agents`` endpoint that runs ``mngr list`` on demand and
returns the current agent list, and a ``/api/agent-info`` endpoint that
returns the current agent's name (from the ``MNGR_AGENT_NAME`` env var).
Designed to be registered on the llm-webchat FastAPI application via the
pluggy ``endpoint`` hook.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from typing import Final

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
from llm_webchat.hookspecs import hookimpl
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_recursive.watcher_common import MngrNotInstalledError
from imbue.mngr_recursive.watcher_common import get_mngr_command

_FETCH_TIMEOUT_SECONDS: Final[float] = 60.0
_FETCH_WARN_THRESHOLD_SECONDS: Final[float] = 15.0
_AGENT_NAME: Final[str] = os.environ.get("MNGR_AGENT_NAME", "")


def _fetch_agent_list(host_name: str) -> list[dict[str, Any]]:
    """Run ``mngr list --format json --quiet`` and return the parsed agent list.

    Filters to the given host if non-empty. Returns an empty list on any error.
    """
    start = time.monotonic()
    try:
        mngr_command = get_mngr_command()
    except MngrNotInstalledError as e:
        logger.debug("Cannot fetch agent list: {}", e)
        return []

    try:
        with ConcurrencyGroup(name="agents-fetch") as cg:
            result = cg.run_process_to_completion(
                [*mngr_command, "list", "--format", "json", "--quiet"],
                timeout=_FETCH_TIMEOUT_SECONDS,
                is_checked_after=False,
            )
    except Exception as e:
        logger.debug("Failed to run mngr list: {}", e)
        return []

    elapsed = time.monotonic() - start
    if elapsed > _FETCH_WARN_THRESHOLD_SECONDS:
        logger.warning("mngr list took {:.1f}s (expected <{:.0f}s)", elapsed, _FETCH_WARN_THRESHOLD_SECONDS)

    if result.is_timed_out:
        logger.warning("mngr list timed out after {}s", _FETCH_TIMEOUT_SECONDS)
        return []

    if result.returncode != 0:
        logger.debug("mngr list failed (exit {}): {}", result.returncode, result.stderr.strip())
        return []

    stdout = result.stdout.strip()
    if not stdout:
        return []

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse mngr list JSON output: {}", e)
        return []

    agents_raw: list[dict[str, Any]] = data.get("agents", [])

    if host_name:
        agents_raw = [a for a in agents_raw if a.get("host", {}).get("name", "") == host_name]

    return agents_raw


def _list_agents_endpoint(request: Request) -> JSONResponse:
    """Handler for GET /api/agents."""
    host_name: str = request.app.state.agents_host_name
    agents = _fetch_agent_list(host_name)
    return JSONResponse(content={"agents": agents})


def _agent_info_endpoint() -> JSONResponse:
    """Handler for GET /api/agent-info."""
    return JSONResponse(content={"name": _AGENT_NAME})


class AgentsPlugin(FrozenModel):
    """Pluggy plugin that registers the /api/agents endpoint."""

    host_name: str = Field(default="", description="Host name to filter agents by")

    @hookimpl
    def endpoint(self, app: FastAPI) -> None:
        app.state.agents_host_name = self.host_name
        app.add_api_route(
            "/api/agents",
            _list_agents_endpoint,
            methods=["GET"],
        )
        app.add_api_route(
            "/api/agent-info",
            _agent_info_endpoint,
            methods=["GET"],
        )
