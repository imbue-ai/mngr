"""Greeting-conversation plugin for the webchat server.

When the user clicks "New conversation", instead of showing an empty chat
form, the frontend JS plugin (``webchat_greeting.js``) calls
``POST /api/greeting-conversation`` to create a conversation pre-populated
with a greeting message from the assistant.  The server runs ``llm inject``
to insert the greeting into the llm database and registers the conversation
in the ``mind_conversations`` table, then returns the new conversation ID
so the frontend can navigate to it.
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from llm_webchat.hookspecs import hookimpl
from loguru import logger
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr_llm.provisioning import MIND_CONVERSATIONS_TABLE_SQL
from imbue.mngr_llm.resources.webchat_plugins.webchat_default_model import read_default_chat_model

_LLM_USER_PATH: Final[str] = os.environ.get("LLM_USER_PATH", "")
_AGENT_WORK_DIR: Final[str] = os.environ.get("MNGR_AGENT_WORK_DIR", "")

_INJECT_TIMEOUT_SECONDS: Final[float] = 30.0
_INJECT_WARN_THRESHOLD_SECONDS: Final[float] = 10.0

_GREETING_MESSAGE: Final[str] = (
    "Hi, I'm Selene. Welcome to the future!\n"
    "\n"
    "> You can interrupt at any time if you want to focus on something else\n"
    "\n"
    "Is it ok if I get to know you a little bit?\n"
    "\n"
    "> This simply generates a document for you to review (to save you time)\n"
    "> \n"
    "> None of your data ever leaves your device."
    " [Learn more](https://imbue.com/help/) about why Imbue is the best"
    " option for privacy and security\n"
)

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
        logger.warning("Failed to open database for greeting conversation registration: {}", exc)
        return
    try:
        conn.execute(MIND_CONVERSATIONS_TABLE_SQL)
        tags = f'{{"name":"{_DEFAULT_CONVERSATION_NAME_TAG}"}}'
        conn.execute(
            "INSERT OR IGNORE INTO mind_conversations (conversation_id, tags, created_at) VALUES (?, ?, ?)",
            (conversation_id, tags, _iso_timestamp()),
        )
        conn.commit()
        logger.debug("Registered greeting conversation {} in mind_conversations", conversation_id)
    except sqlite3.Error as exc:
        logger.warning("Failed to register greeting conversation {}: {}", conversation_id, exc)
    finally:
        conn.close()


def _parse_conversation_id_from_inject_output(stdout: str) -> str | None:
    """Parse the conversation ID from ``llm inject`` output.

    The output format is: ``Injected message into conversation <id>``
    """
    stripped = stdout.strip()
    parts = stripped.rsplit(" ", 1)
    if len(parts) == 2:
        return parts[1]
    return None


def _create_greeting_conversation(agent_work_dir: str, llm_user_path: str) -> str | None:
    """Create a new conversation with a greeting message via ``llm inject``.

    Returns the conversation ID on success, or None on failure.
    """
    model_id = read_default_chat_model(agent_work_dir)
    cmd = ["llm", "inject", "-m", model_id, "--prompt", _DEFAULT_CONVERSATION_NAME_TAG, _GREETING_MESSAGE]

    env = dict(os.environ)
    if llm_user_path:
        env["LLM_USER_PATH"] = llm_user_path

    logger.debug("Creating greeting conversation with model={}", model_id)
    start = time.monotonic()

    try:
        with ConcurrencyGroup(name="webchat-greeting") as cg:
            result = cg.run_process_to_completion(
                cmd,
                timeout=_INJECT_TIMEOUT_SECONDS,
                is_checked_after=False,
                env=env,
            )
    except FileNotFoundError:
        logger.warning("llm command not found; cannot create greeting conversation")
        return None

    elapsed = time.monotonic() - start
    if elapsed > _INJECT_WARN_THRESHOLD_SECONDS:
        logger.warning(
            "llm inject took {:.1f}s (expected <{:.0f}s)",
            elapsed,
            _INJECT_WARN_THRESHOLD_SECONDS,
        )

    if result.returncode != 0:
        logger.warning("llm inject failed (exit {}): {}", result.returncode, result.stderr.strip())
        return None

    conversation_id = _parse_conversation_id_from_inject_output(result.stdout)
    if conversation_id is None:
        logger.warning("Could not parse conversation ID from llm inject output: {}", result.stdout.strip())
        return None

    logger.debug("Created greeting conversation {}", conversation_id)

    # Register in mind_conversations table
    if llm_user_path:
        db_path = Path(llm_user_path) / "logs.db"
        _register_conversation_in_mind_table(db_path, conversation_id)

    return conversation_id


def _greeting_conversation_endpoint() -> JSONResponse:
    """Handler for POST /api/greeting-conversation."""
    conversation_id = _create_greeting_conversation(
        agent_work_dir=_AGENT_WORK_DIR,
        llm_user_path=_LLM_USER_PATH,
    )
    if conversation_id is None:
        return JSONResponse(
            content={"error": "Failed to create greeting conversation"},
            status_code=500,
        )
    return JSONResponse(
        content={"conversation_id": conversation_id},
        status_code=201,
    )


class GreetingPlugin(FrozenModel):
    """Pluggy plugin that registers the /api/greeting-conversation endpoint."""

    agent_work_dir: str = Field(default="", description="Agent work directory containing minds.toml")
    llm_user_path: str = Field(default="", description="Path to the llm user directory")

    @hookimpl
    def endpoint(self, app: FastAPI) -> None:
        app.add_api_route(
            "/api/greeting-conversation",
            _greeting_conversation_endpoint,
            methods=["POST"],
        )
