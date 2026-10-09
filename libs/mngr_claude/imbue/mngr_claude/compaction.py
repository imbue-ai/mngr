from __future__ import annotations

from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.mngr.agents.compaction_transcript import COMPACTION_TRANSCRIPT_CACHE
from imbue.mngr.agents.compaction_transcript import CompactionTranscriptReading
from imbue.mngr.agents.compaction_transcript import CompactionTranscriptScanner
from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.errors import MngrError
from imbue.mngr_claude.claude_config import IDLE_SINCE_FILENAME
from imbue.mngr_claude.claude_config import LAST_COMPACTED_IDLE_SINCE_FILENAME

CLAUDE_DEFAULT_CACHE_TTL_MINUTES: int = 60

# Searched in this order for both the idle timestamp and the context token count.
_CLAUDE_TRANSCRIPT_RELATIVE_PATHS: Final[tuple[str, ...]] = (
    "logs/claude_transcript/events.jsonl",
    "events/claude/common_transcript/events.jsonl",
    "transcript.jsonl",
)


def parse_iso_timestamp(timestamp_str: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string into a timezone-aware UTC datetime."""
    try:
        clean_str = timestamp_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError, IndexError):
        return None


def extract_assistant_timestamp_from_record(record: dict[str, Any]) -> datetime | None:
    """Return the timestamp of a transcript record if it is an assistant turn with a parseable one."""
    if record.get("type") not in ("assistant", "assistant_message"):
        return None
    ts_str = record.get("timestamp")
    return parse_iso_timestamp(ts_str) if isinstance(ts_str, str) else None


def extract_context_tokens_from_record(record: dict[str, Any]) -> int | None:
    """Return a transcript record's prompt context token count, or None if it reports none (or zero)."""
    usage: dict[str, object] | None = None
    event_type = record.get("type")
    if event_type in ("assistant", "assistant_message"):
        msg = record.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
            usage = msg["usage"]
        elif isinstance(record.get("usage"), dict):
            usage = record["usage"]
        else:
            usage = None
    elif isinstance(record.get("usage"), dict):
        usage = record["usage"]
    else:
        usage = None

    if usage is None:
        return None
    try:
        input_tokens = int(usage.get("input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or usage.get("cache_write_tokens") or 0)
    except (ValueError, TypeError):
        return None
    total = input_tokens + cache_read + cache_write
    return total if total > 0 else None


def is_compact_boundary_record(record: dict[str, Any]) -> bool:
    """Whether a transcript record is the boundary Claude writes when it compacts the conversation."""
    return record.get("type") == "system" and record.get("subtype") == "compact_boundary"


class ClaudeCompactionTranscriptScanner(CompactionTranscriptScanner):
    """Finds the newest assistant-turn timestamp and context token count in a Claude transcript.

    A compact boundary newer than every usage leaves the context size unknown until the next reply.
    The boundary's own post-compaction estimate is not used: it leaves out the session's fixed
    overhead, so the next reply's real usage runs several times larger.
    """

    def visit_older_record(self, record: dict[str, Any]) -> None:
        if self.latest_assistant_timestamp is None:
            self.latest_assistant_timestamp = extract_assistant_timestamp_from_record(record)
        if not self.is_context_size_settled():
            if is_compact_boundary_record(record):
                self.is_context_size_unknown_since_compaction = True
            else:
                self.latest_context_tokens = extract_context_tokens_from_record(record)


def _read_claude_transcript(agent: InteractiveTuiAgent, transcript_path: Path) -> CompactionTranscriptReading | None:
    return COMPACTION_TRANSCRIPT_CACHE.read_transcript(
        host=agent.host,
        host_id=agent.host_id,
        agent_id=agent.id,
        transcript_path=transcript_path,
        scanner_factory=ClaudeCompactionTranscriptScanner,
    )


def get_agent_idle_since(agent: InteractiveTuiAgent) -> datetime | None:
    """Return the datetime when the agent entered idle state, or None if currently active/unknown."""
    try:
        agent_dir = agent._get_agent_dir()
        if agent.host.path_exists(agent_dir / "active"):
            return None

        idle_since_dt: datetime | None = None

        # Prefer the timestamp of the latest assistant turn in the transcript
        for rel_path in _CLAUDE_TRANSCRIPT_RELATIVE_PATHS:
            reading = _read_claude_transcript(agent, agent_dir / rel_path)
            if reading is not None and reading.latest_assistant_timestamp is not None:
                idle_since_dt = reading.latest_assistant_timestamp
                break

        if idle_since_dt is None:
            idle_since_path = agent_dir / IDLE_SINCE_FILENAME
            if agent.host.path_exists(idle_since_path):
                raw = agent.host.read_text_file(idle_since_path)
                idle_since_dt = parse_iso_timestamp(raw)

        if idle_since_dt is None:
            for fallback_rel_path in [
                "session_started",
                "claude_process_started",
            ]:
                fallback_path = agent_dir / fallback_rel_path
                if agent.host.path_exists(fallback_path):
                    mtime = agent.host.get_file_mtime(fallback_path)
                    if mtime is not None:
                        if mtime.tzinfo is None:
                            mtime = mtime.replace(tzinfo=timezone.utc)
                        if idle_since_dt is None or mtime > idle_since_dt:
                            idle_since_dt = mtime

        if idle_since_dt is not None:
            last_compacted = get_agent_last_compacted_idle_since(agent)
            if last_compacted is not None and last_compacted >= idle_since_dt:
                return None

            return idle_since_dt
        return None
    except (MngrError, OSError, ValueError, KeyError, AttributeError) as e:
        logger.debug("Failed resolving idle_since timestamp for agent {}: {}", agent.name, e)
        return None


def get_agent_last_compacted_idle_since(agent: InteractiveTuiAgent) -> datetime | None:
    """Return the idle_since timestamp for which the agent was last compacted, or None."""
    try:
        agent_dir = agent._get_agent_dir()
        path = agent_dir / LAST_COMPACTED_IDLE_SINCE_FILENAME
        if agent.host.path_exists(path):
            raw = agent.host.read_text_file(path)
            return parse_iso_timestamp(raw)
        return None
    except (MngrError, OSError, ValueError, KeyError, AttributeError) as e:
        logger.debug("Failed reading last_compacted_idle_since for agent {}: {}", agent.name, e)
        return None


def record_agent_compacted(agent: InteractiveTuiAgent, idle_since: datetime | None = None) -> None:
    """Record that compaction was executed for the agent's current idle epoch."""
    agent_dir = agent._get_agent_dir()
    ts = idle_since or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    iso_str = ts.isoformat()
    agent.host.write_text_file(agent_dir / LAST_COMPACTED_IDLE_SINCE_FILENAME, iso_str)
    agent.host.write_text_file(agent_dir / IDLE_SINCE_FILENAME, iso_str)


def get_agent_context_tokens(agent: InteractiveTuiAgent) -> int | None:
    """Return the total prompt context token count from the agent's most recent turn, or None if unknown.

    The count is unknown after a compaction until the agent's next reply reports one.
    """
    try:
        agent_dir = agent._get_agent_dir()
        for rel_path in _CLAUDE_TRANSCRIPT_RELATIVE_PATHS:
            transcript_path = agent_dir / rel_path
            reading = _read_claude_transcript(agent, transcript_path)
            if reading is not None and reading.is_context_size_settled():
                if reading.is_context_size_unknown_since_compaction:
                    logger.debug(
                        "Context size of agent {} is unknown until its next reply: {} has a compaction newer than any usage",
                        agent.name,
                        transcript_path,
                    )
                return reading.latest_context_tokens
        return None
    except (MngrError, OSError, ValueError, KeyError, AttributeError) as e:
        logger.debug("Failed resolving context token count for agent {}: {}", agent.name, e)
        return None
