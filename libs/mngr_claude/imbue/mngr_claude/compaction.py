from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone

from loguru import logger

from imbue.mngr.agents.tui_agent import InteractiveTuiAgent
from imbue.mngr.errors import MngrError
from imbue.mngr_claude.claude_config import IDLE_SINCE_FILENAME
from imbue.mngr_claude.claude_config import LAST_COMPACTED_IDLE_SINCE_FILENAME

CLAUDE_DEFAULT_CACHE_TTL_MINUTES: int = 60


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


def extract_latest_assistant_timestamp_from_jsonl(raw_text: str) -> datetime | None:
    """Extract timestamp of the most recent assistant turn in a JSONL transcript."""
    if not raw_text:
        return None
    lines = raw_text.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("Failed to parse JSONL line when extracting assistant timestamp: {}", e)
            continue
        if not isinstance(record, dict):
            continue

        event_type = record.get("type")
        if event_type in ("assistant", "assistant_message"):
            ts_str = record.get("timestamp")
            if isinstance(ts_str, str):
                dt = parse_iso_timestamp(ts_str)
                if dt is not None:
                    return dt
    return None


def get_agent_idle_since(agent: InteractiveTuiAgent) -> datetime | None:
    """Return the datetime when the agent entered idle state, or None if currently active/unknown."""
    try:
        agent_dir = agent._get_agent_dir()
        if agent.host.path_exists(agent_dir / "active"):
            return None

        idle_since_dt: datetime | None = None

        # Prefer the timestamp of the latest assistant turn in the transcript
        for rel_path in [
            "logs/claude_transcript/events.jsonl",
            "events/claude/common_transcript/events.jsonl",
            "transcript.jsonl",
        ]:
            transcript_path = agent_dir / rel_path
            if agent.host.path_exists(transcript_path):
                content = agent.host.read_text_file(transcript_path)
                ts = extract_latest_assistant_timestamp_from_jsonl(content)
                if ts is not None:
                    idle_since_dt = ts
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


def extract_context_tokens_from_jsonl(raw_text: str) -> int | None:
    """Extract prompt context token count from the most recent assistant turn in a JSONL transcript."""
    if not raw_text:
        return None
    lines = raw_text.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("Failed parsing line in JSONL transcript: {}", e)
            continue
        if not isinstance(record, dict):
            continue

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

        if usage is not None:
            try:
                input_tokens = int(usage.get("input_tokens") or 0)
                cache_read = int(usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0)
                cache_write = int(usage.get("cache_creation_input_tokens") or usage.get("cache_write_tokens") or 0)
                total = input_tokens + cache_read + cache_write
                if total > 0:
                    return total
            except (ValueError, TypeError):
                continue
    return None


def get_agent_context_tokens(agent: InteractiveTuiAgent) -> int | None:
    """Return the total prompt context token count from the agent's most recent turn, or None if unknown."""
    try:
        agent_dir = agent._get_agent_dir()
        for rel_path in [
            "logs/claude_transcript/events.jsonl",
            "events/claude/common_transcript/events.jsonl",
            "transcript.jsonl",
        ]:
            transcript_path = agent_dir / rel_path
            if agent.host.path_exists(transcript_path):
                content = agent.host.read_text_file(transcript_path)
                tokens = extract_context_tokens_from_jsonl(content)
                if tokens is not None:
                    return tokens
        return None
    except (MngrError, OSError, ValueError, KeyError, AttributeError) as e:
        logger.debug("Failed resolving context token count for agent {}: {}", agent.name, e)
        return None
