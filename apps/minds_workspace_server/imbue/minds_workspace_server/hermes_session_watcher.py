"""Watch hermes SQLite session store for new events.

Hermes stores conversation history in ``$HERMES_HOME/state.db`` (SQLite WAL
mode) -- one row per message, keyed by an auto-increment integer. This
watcher polls for rows with id greater than the last seen id and translates
each row into the same common-transcript event shape produced by
``session_parser.py`` for Claude, so the frontend renders hermes replies the
same way as claude ones.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Final

from loguru import logger as _loguru_logger

from imbue.mngr.utils.env_utils import parse_env_file

logger = _loguru_logger

_HERMES_HOME_DIR_NAME: Final[str] = "hermes_home"
_STATE_DB_FILENAME: Final[str] = "state.db"
_SOURCE: Final[str] = "hermes/sqlite"
_POLL_INTERVAL_SECONDS: Final[float] = 1.0
_MAX_OUTPUT_LENGTH: Final[int] = 2000
_MAX_INPUT_PREVIEW_LENGTH: Final[int] = 200


def _resolve_hermes_home(agent_state_dir: Path) -> Path:
    """Return the per-agent HERMES_HOME path.

    Prefers ``HERMES_HOME`` from the agent's env file (this matches what
    mngr_hermes actually injected into the agent process), falling back to
    the conventional per-agent location so the watcher still works if the
    env file hasn't been written yet (e.g. during a brief startup window).
    """
    env_file = agent_state_dir / "env"
    if env_file.exists():
        try:
            env_vars = parse_env_file(env_file.read_text())
            hermes_home = env_vars.get("HERMES_HOME")
            if hermes_home:
                return Path(hermes_home)
        except OSError:
            logger.debug("Failed to read env file: {}", env_file)
    return agent_state_dir / _HERMES_HOME_DIR_NAME


def _format_timestamp(unix_epoch_seconds: float) -> str:
    """Format a unix epoch as an ISO 8601 UTC string to match claude's format."""
    return datetime.fromtimestamp(unix_epoch_seconds, tz=timezone.utc).isoformat()


def _truncate(text: str, max_length: int) -> str:
    if len(text) > max_length:
        return text[:max_length] + "..."
    return text


def _build_tool_calls_preview(tool_calls_json: str | None) -> list[dict[str, str]]:
    """Translate hermes's OpenAI-format tool_calls JSON into the claude preview shape.

    Claude emits each tool call as ``{tool_call_id, tool_name, input_preview}``.
    Hermes stores OpenAI's shape: ``[{"id", "type": "function", "function":
    {"name", "arguments": "<json string>"}}]``. Arguments arrive as a JSON
    string; if it doesn't parse we preserve the raw text so the UI still
    has something to show.
    """
    if not tool_calls_json:
        return []
    try:
        parsed = json.loads(tool_calls_json)
    except json.JSONDecodeError:
        logger.debug("Failed to parse tool_calls JSON")
        return []
    if not isinstance(parsed, list):
        return []

    result: list[dict[str, str]] = []
    for tool_call in parsed:
        if not isinstance(tool_call, dict):
            continue
        func = tool_call.get("function", {})
        if not isinstance(func, dict):
            func = {}
        tool_name = str(func.get("name", ""))
        call_id = str(tool_call.get("id", ""))
        args_raw = func.get("arguments", "")
        if isinstance(args_raw, str):
            try:
                args_obj: Any = json.loads(args_raw) if args_raw else {}
            except json.JSONDecodeError:
                args_obj = {"_raw": args_raw}
        else:
            args_obj = args_raw
        input_preview = _truncate(json.dumps(args_obj, separators=(",", ":")), _MAX_INPUT_PREVIEW_LENGTH)
        result.append(
            {
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "input_preview": input_preview,
            }
        )
    return result


def _translate_row(row: sqlite3.Row) -> dict[str, Any] | None:
    """Translate one hermes `messages` row into a common-transcript event.

    Returns None for rows that don't map to a user-visible event (system
    messages, empty content, etc.).
    """
    role = row["role"]
    content = row["content"] or ""
    msg_id = row["id"]
    session_id = row["session_id"]
    timestamp = _format_timestamp(row["timestamp"])

    if role == "user":
        if not content:
            return None
        return {
            "timestamp": timestamp,
            "type": "user_message",
            "event_id": f"hermes-msg-{msg_id}-user",
            "source": _SOURCE,
            "role": "user",
            "content": content,
            "message_uuid": f"hermes-msg-{msg_id}",
            "session_id": session_id,
        }

    if role == "assistant":
        tool_calls = _build_tool_calls_preview(row["tool_calls"])
        # Skip assistant rows with no visible content and no tool calls
        if not content and not tool_calls:
            return None
        return {
            "timestamp": timestamp,
            "type": "assistant_message",
            "event_id": f"hermes-msg-{msg_id}-assistant",
            "source": _SOURCE,
            "role": "assistant",
            "model": "",
            "text": content,
            "tool_calls": tool_calls,
            "stop_reason": row["finish_reason"],
            "usage": None,
            "message_uuid": f"hermes-msg-{msg_id}",
            "session_id": session_id,
        }

    if role == "tool":
        tool_call_id = row["tool_call_id"] or ""
        tool_name = row["tool_name"] or "unknown"
        return {
            "timestamp": timestamp,
            "type": "tool_result",
            "event_id": f"hermes-msg-{msg_id}-tool_result-{tool_call_id}",
            "source": _SOURCE,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "output": _truncate(content, _MAX_OUTPUT_LENGTH),
            "is_error": False,
            "message_uuid": f"hermes-msg-{msg_id}",
            "session_id": session_id,
        }

    return None


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the hermes SQLite DB in read-only mode with row-based access."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class HermesSessionWatcher:
    """Watches a hermes per-agent SQLite session store and emits parsed events.

    Matches the public interface of ``AgentSessionWatcher`` so the server can
    swap between them based on agent type.
    """

    def __init__(
        self,
        agent_id: str,
        agent_state_dir: Path,
        on_events: Callable[[str, list[dict[str, Any]]], None],
    ) -> None:
        self._agent_id = agent_id
        self._db_path = _resolve_hermes_home(agent_state_dir) / _STATE_DB_FILENAME
        self._on_events = on_events

        self._last_message_id: int = 0
        self._existing_event_ids: set[str] = set()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"hermes-watcher-{self._agent_id}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Read every message for this agent (optionally filtered to one session).

        Also updates ``_last_message_id`` so the next polling cycle only
        reports messages newer than what we already returned here.
        """
        if not self._db_path.exists():
            return []

        try:
            conn = _open_readonly(self._db_path)
        except sqlite3.OperationalError:
            return []

        try:
            if session_id is not None:
                rows = conn.execute(
                    "SELECT id, session_id, role, content, tool_call_id, tool_calls, "
                    "tool_name, timestamp, finish_reason "
                    "FROM messages WHERE session_id = ? ORDER BY id ASC",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, session_id, role, content, tool_call_id, tool_calls, "
                    "tool_name, timestamp, finish_reason "
                    "FROM messages WHERE session_id IN "
                    "(SELECT id FROM sessions WHERE source = 'cli') "
                    "ORDER BY id ASC"
                ).fetchall()
        finally:
            conn.close()

        events: list[dict[str, Any]] = []
        for row in rows:
            event = _translate_row(row)
            if event is None:
                continue
            self._existing_event_ids.add(event["event_id"])
            events.append(event)
            if row["id"] > self._last_message_id:
                self._last_message_id = row["id"]

        return events

    def get_backfill_events(
        self, before_event_id: str, limit: int = 50, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return events immediately before a given event_id for pagination."""
        all_events = self.get_all_events(session_id=session_id)

        target_idx = -1
        for i, event in enumerate(all_events):
            if event["event_id"] == before_event_id:
                target_idx = i
                break

        if target_idx <= 0:
            return []

        return all_events[max(0, target_idx - limit) : target_idx]

    def get_subagent_metadata(self, subagent_session_id: str) -> dict[str, str] | None:
        """Hermes has no equivalent of Claude's agent-tool subagents; always None."""
        return None

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=_POLL_INTERVAL_SECONDS):
            try:
                new_events = self._poll_for_new_events()
            except (sqlite3.Error, OSError):
                logger.exception("Hermes watcher {} poll failed", self._agent_id)
                continue
            if new_events:
                self._on_events(self._agent_id, new_events)

    def _poll_for_new_events(self) -> list[dict[str, Any]]:
        if not self._db_path.exists():
            return []

        try:
            conn = _open_readonly(self._db_path)
        except sqlite3.OperationalError:
            return []

        try:
            rows = conn.execute(
                "SELECT id, session_id, role, content, tool_call_id, tool_calls, "
                "tool_name, timestamp, finish_reason "
                "FROM messages WHERE id > ? AND session_id IN "
                "(SELECT id FROM sessions WHERE source = 'cli') "
                "ORDER BY id ASC",
                (self._last_message_id,),
            ).fetchall()
        finally:
            conn.close()

        new_events: list[dict[str, Any]] = []
        for row in rows:
            event = _translate_row(row)
            if row["id"] > self._last_message_id:
                self._last_message_id = row["id"]
            if event is None:
                continue
            if event["event_id"] in self._existing_event_ids:
                continue
            self._existing_event_ids.add(event["event_id"])
            new_events.append(event)
        return new_events
