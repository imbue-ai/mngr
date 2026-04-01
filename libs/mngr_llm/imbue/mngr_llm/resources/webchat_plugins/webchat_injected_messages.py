"""Injected-message watcher plugin for the webchat server.

Polls the llm database for new responses that appear outside of the normal
``llm prompt`` flow (e.g. via ``llm inject``) and broadcasts the full response
data into the llm-webchat event stream so that connected frontends can insert
them directly via ``$llm.insertResponse`` without a page refresh.

Injected messages are detected by their prompt column: ``llm inject`` creates
responses with an empty prompt (``""``), whereas normal ``llm prompt`` always
has a non-empty user prompt. Preliminary rows from ``llm live-chat``
(``response=""``) are ignored.

Designed to be registered on the llm-webchat application via the pluggy
``register_event_broadcaster`` hook.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

from llm_webchat.events import BufferBehavior
from llm_webchat.hookspecs import hookimpl
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

_POLL_INTERVAL_SECONDS: Final[float] = 2.0

# Event type broadcast when an injected message is detected.
# The frontend JS plugin listens for this and calls $llm.insertResponse.
_INJECTED_MESSAGE_EVENT_TYPE: Final[str] = "injected_message"


class InjectedResponseData(FrozenModel):
    """Response data included in the injected_message event.

    Mirrors the shape of ``llm_webchat.models.ResponseItem`` so the frontend
    can pass it directly to ``$llm.insertResponse``.
    """

    id: str
    model: str
    prompt: str | None
    system: str | None
    response: str
    conversation_id: str
    datetime_utc: str
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None


def _is_injected_response(prompt: str | None, response: str | None) -> bool:
    """Return True if a response row looks like it was created by ``llm inject``.

    Heuristic: ``llm inject`` creates responses with an empty prompt and a
    non-empty response. Normal ``llm prompt`` always has a non-empty user
    prompt. Preliminary rows from ``llm live-chat`` have ``response=""``,
    so those are excluded.
    """
    is_prompt_empty = not prompt or prompt.strip() == ""
    is_response_present = bool(response and response.strip())
    return is_prompt_empty and is_response_present


def _get_max_rowid(db_path: Path) -> int:
    """Return the current max rowid in the responses table, or 0 if unavailable."""
    if not db_path.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.debug("Cannot open database for max rowid: {}", exc)
        return 0
    try:
        row = conn.execute("SELECT MAX(rowid) FROM responses").fetchone()
        return row[0] or 0 if row else 0
    except sqlite3.Error as exc:
        logger.debug("Cannot query max rowid: {}", exc)
        return 0
    finally:
        conn.close()


def _get_tracked_conversation_ids(db_path: Path) -> set[str]:
    """Read tracked conversation IDs from the mind_conversations table."""
    if not db_path.is_file():
        return set()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.debug("Cannot open database for tracked conversations: {}", exc)
        return set()
    try:
        rows = conn.execute("SELECT conversation_id FROM mind_conversations").fetchall()
        return {row[0] for row in rows}
    except sqlite3.Error as exc:
        logger.debug("mind_conversations table not available: {}", exc)
        return set()
    finally:
        conn.close()


def _poll_for_injected_messages(
    db_path: Path,
    after_rowid: int,
    tracked_conversation_ids: set[str],
) -> tuple[list[InjectedResponseData], int]:
    """Find injected responses that appeared after the given rowid.

    Returns a list of response data objects and the new max rowid.
    Each injected response produces its own entry (no deduplication by
    conversation ID) so the frontend can insert each one individually.
    """
    if not db_path.is_file():
        return [], after_rowid
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.debug("Cannot open database for polling: {}", exc)
        return [], after_rowid

    new_max = after_rowid
    injected_responses: list[InjectedResponseData] = []
    try:
        rows = conn.execute(
            "SELECT rowid, id, model, prompt, system, response, conversation_id, "
            "datetime_utc, duration_ms, input_tokens, output_tokens "
            "FROM responses WHERE rowid > ? ORDER BY rowid ASC",
            (after_rowid,),
        ).fetchall()
        for row in rows:
            rowid = row[0]
            new_max = max(new_max, rowid)
            conversation_id = row[6]
            prompt = row[3]
            response = row[5]
            if conversation_id not in tracked_conversation_ids:
                continue
            if not _is_injected_response(prompt, response):
                continue
            injected_responses.append(
                InjectedResponseData(
                    id=row[1] or "",
                    model=row[2] or "",
                    prompt=prompt,
                    system=row[4],
                    response=response or "",
                    conversation_id=conversation_id,
                    datetime_utc=row[7] or "",
                    duration_ms=row[8],
                    input_tokens=row[9],
                    output_tokens=row[10],
                )
            )
    except sqlite3.Error as exc:
        logger.debug("Error polling for injected messages: {}", exc)
    finally:
        conn.close()
    return injected_responses, new_max


def _run_poll_loop(
    db_path: Path,
    broadcaster: Callable[[str, dict[str, Any]], None],
    stop_event: threading.Event,
) -> None:
    """Background thread: poll the DB for injected messages and broadcast notifications."""
    last_rowid = _get_max_rowid(db_path)
    logger.debug("Injected-message watcher started, initial rowid={}", last_rowid)

    while not stop_event.is_set():
        stop_event.wait(_POLL_INTERVAL_SECONDS)
        if stop_event.is_set():
            break

        tracked_ids = _get_tracked_conversation_ids(db_path)
        if not tracked_ids:
            continue

        injected_responses, new_max = _poll_for_injected_messages(db_path, last_rowid, tracked_ids)
        for response_data in injected_responses:
            logger.debug(
                "Detected injected message {} in conversation {}",
                response_data.id,
                response_data.conversation_id,
            )
            # The llm-webchat frontend only passes ``type``, ``content``,
            # and ``model`` from raw SSE events into the ``stream_event``
            # hook payload.  To get the full response data through to our
            # JS plugin, we JSON-encode it into the ``content`` field.
            broadcaster(
                response_data.conversation_id,
                {
                    "type": _INJECTED_MESSAGE_EVENT_TYPE,
                    "content": json.dumps(response_data.model_dump()),
                    "buffer_behavior": BufferBehavior.IGNORE,
                },
            )

        if new_max > last_rowid:
            last_rowid = new_max

    logger.debug("Injected-message watcher stopped")


class InjectedMessagesPlugin(FrozenModel):
    """Pluggy plugin that watches for injected messages and broadcasts notifications."""

    db_path: Path = Field(description="Path to the llm logs.db database")

    @hookimpl
    def register_event_broadcaster(self, broadcaster: Callable[[str, dict[str, Any]], None]) -> None:
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_run_poll_loop,
            args=(self.db_path, broadcaster, stop_event),
            daemon=True,
        )
        thread.start()
