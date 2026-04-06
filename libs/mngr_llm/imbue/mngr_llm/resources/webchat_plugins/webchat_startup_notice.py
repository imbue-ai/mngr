"""Startup notice endpoint for the webchat server.

Exposes ``GET /api/agent-startup-status`` which checks whether the
thinking agent's Claude Code session has started.  The companion
JavaScript plugin (``webchat_startup_notice.js``) polls this endpoint
and shows a banner when the session has not yet started, prompting
the user to connect via ``mngr connect`` to resolve any blocking
startup dialogs (e.g. the bypass-permissions confirmation).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
from llm_webchat.hookspecs import hookimpl
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

_AGENT_STATE_DIR: Final[str] = os.environ.get("MNGR_AGENT_STATE_DIR", "")
_AGENT_NAME: Final[str] = os.environ.get("MNGR_AGENT_NAME", "")


def is_session_started(agent_state_dir: str) -> bool:
    """Check whether the Claude Code session has started.

    The SessionStart hook creates a ``session_started`` file in the agent
    state directory when the session begins.  Its absence means the agent
    is still blocked on a startup dialog.
    """
    if not agent_state_dir:
        return True
    return Path(agent_state_dir, "session_started").exists()


def _startup_status_endpoint(request: Request) -> JSONResponse:
    """Handler for GET /api/agent-startup-status."""
    agent_state_dir: str = request.app.state.startup_notice_state_dir
    agent_name: str = request.app.state.startup_notice_agent_name
    started = is_session_started(agent_state_dir)
    return JSONResponse(content={"started": started, "agent_name": agent_name})


class StartupNoticePlugin(FrozenModel):
    """Pluggy plugin that registers the /api/agent-startup-status endpoint."""

    agent_state_dir: str = Field(default=_AGENT_STATE_DIR, description="Agent state directory to check for session_started")
    agent_name: str = Field(default=_AGENT_NAME, description="Agent name shown in the connect command")

    @hookimpl
    def endpoint(self, app: FastAPI) -> None:
        app.state.startup_notice_state_dir = self.agent_state_dir
        app.state.startup_notice_agent_name = self.agent_name
        app.add_api_route(
            "/api/agent-startup-status",
            _startup_status_endpoint,
            methods=["GET"],
        )
