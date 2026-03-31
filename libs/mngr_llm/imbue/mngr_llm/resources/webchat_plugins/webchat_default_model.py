"""Default chat model endpoint for the webchat server.

Reads the default chat model from ``$MNGR_AGENT_WORK_DIR/minds.toml``
(falling back to ``claude-haiku-4.5``) and exposes it via
``GET /api/default-model``.  The companion JavaScript plugin
(``webchat_default_model.js``) fetches this value on page load and seeds
the frontend's model selector when the user has not yet made a choice.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from llm_webchat.hookspecs import hookimpl
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

_AGENT_WORK_DIR: Final[str] = os.environ.get("MNGR_AGENT_WORK_DIR", "")
_FALLBACK_MODEL: Final[str] = "claude-haiku-4.5"


def read_default_chat_model(agent_work_dir: str) -> str:
    """Read the default chat model from minds.toml, falling back to claude-haiku-4.5."""
    if not agent_work_dir:
        return _FALLBACK_MODEL
    settings_path = Path(agent_work_dir) / "minds.toml"
    try:
        if settings_path.exists():
            raw = tomllib.loads(settings_path.read_text())
            model = raw.get("chat", {}).get("model")
            if model:
                return str(model)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.debug("Failed to load chat model from minds.toml: {}", e)
    return _FALLBACK_MODEL


def _default_model_endpoint() -> JSONResponse:
    """Handler for GET /api/default-model."""
    model_id = read_default_chat_model(_AGENT_WORK_DIR)
    return JSONResponse(content={"model_id": model_id})


class DefaultModelPlugin(FrozenModel):
    """Pluggy plugin that registers the /api/default-model endpoint."""

    agent_work_dir: str = Field(default="", description="Agent work directory containing minds.toml")

    @hookimpl
    def endpoint(self, app: FastAPI) -> None:
        app.add_api_route(
            "/api/default-model",
            _default_model_endpoint,
            methods=["GET"],
        )
