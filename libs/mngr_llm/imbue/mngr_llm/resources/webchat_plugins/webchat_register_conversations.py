"""Register-conversations plugin for the webchat server.

When llm-webchat creates a new conversation (via ``POST /api/conversations``),
it only inserts into the ``conversations`` table. This plugin overrides that
endpoint so that the conversation is also registered in the
``mind_conversations`` table, which the rest of the mngr ecosystem uses for
conversation tracking (e.g. the conversation watcher, injected-message
watcher, and the mind chat UI).

The ``mind_conversations`` table is the authoritative source for "which
conversations belong to this mind". Without registration, conversations
created through the webchat UI would be invisible to other components.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
from llm_webchat.database import create_conversation
from llm_webchat.hookspecs import hookimpl
from llm_webchat.models import CreateConversationRequest
from llm_webchat.models import CreateConversationResponse
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_llm.provisioning import MIND_CONVERSATIONS_TABLE_SQL

_DEFAULT_CONVERSATION_NAME_TAG: Final[str] = "(new chat)"


def _iso_timestamp() -> str:
    """Return the current UTC time as an ISO 8601 timestamp with nanosecond precision."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond * 1000:09d}Z"


def _register_conversation_in_mind_table(db_path: Path, conversation_id: str) -> None:
    """Insert a conversation record into the mind_conversations table.

    Creates the table if it does not exist. Ignores duplicates.
    """
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as exc:
        logger.warning("Failed to open database for conversation registration: {}", exc)
        return
    try:
        conn.execute(MIND_CONVERSATIONS_TABLE_SQL)
        tags = f'{{"name":"{_DEFAULT_CONVERSATION_NAME_TAG}"}}'
        conn.execute(
            "INSERT OR IGNORE INTO mind_conversations (conversation_id, tags, created_at) VALUES (?, ?, ?)",
            (conversation_id, tags, _iso_timestamp()),
        )
        conn.commit()
        logger.debug("Registered conversation {} in mind_conversations", conversation_id)
    except sqlite3.Error as exc:
        logger.warning("Failed to register conversation {}: {}", conversation_id, exc)
    finally:
        conn.close()


def _create_conversation_with_registration(
    create_conversation_request: CreateConversationRequest,
    request: Request,
) -> JSONResponse:
    """Create a conversation and register it in the mind_conversations table."""
    database = request.app.state.database
    conversation = create_conversation(database, create_conversation_request.name, create_conversation_request.model)

    db_path: Path = request.app.state.mind_conversations_db_path
    _register_conversation_in_mind_table(db_path, conversation.id)

    response = CreateConversationResponse(id=conversation.id)
    return JSONResponse(content=response.model_dump(), status_code=201)


class RegisterConversationsPlugin(FrozenModel):
    """Pluggy plugin that registers new conversations in the mind_conversations table."""

    db_path: Path = Field(description="Path to the llm logs.db database")

    @hookimpl
    def endpoint(self, app: FastAPI) -> None:
        app.state.mind_conversations_db_path = self.db_path
        app.add_api_route(
            "/api/conversations",
            _create_conversation_with_registration,
            methods=["POST"],
        )
