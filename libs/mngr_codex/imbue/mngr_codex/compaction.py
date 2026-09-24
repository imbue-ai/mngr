from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentLifecycleState
from imbue.mngr_codex.codex_config import COMMON_TRANSCRIPT_OUTPUT_RELATIVE
from imbue.mngr_codex.codex_config import IDLE_SINCE_FILENAME
from imbue.mngr_codex.codex_config import LAST_COMPACTED_IDLE_SINCE_FILENAME
from imbue.mngr_codex.codex_config import RAW_TRANSCRIPT_OUTPUT_RELATIVE
from imbue.mngr_codex.codex_config import TRANSCRIPT_PATH_FILENAME

CODEX_DEFAULT_CACHE_TTL_MINUTES: Final[int] = 30


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
    """Extract timestamp of the most recent assistant turn/activity in a JSONL transcript."""
    if not raw_text:
        return None
    lines = raw_text.strip().splitlines()
    compaction_turn_ids: set[str] = set()

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
        raw_payload = record.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}

        # Compaction records and usage telemetry are not assistant activity
        if event_type == "compacted":
            if isinstance(raw_payload, dict):
                tur = raw_payload.get("latest_token_usage_record")
                if isinstance(tur, dict) and tur.get("turn_id"):
                    compaction_turn_ids.add(str(tur["turn_id"]))
            continue

        if event_type in ("token_usage_record", "token_count"):
            continue

        if payload.get("type") == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "ContextCompaction":
                tid = payload.get("turn_id")
                if tid:
                    compaction_turn_ids.add(str(tid))
            continue

        is_assistant_activity = False
        if event_type == "event_msg":
            ptype = payload.get("type")
            if ptype == "agent_message":
                is_assistant_activity = True
            elif ptype == "task_complete":
                turn_id = payload.get("turn_id")
                if turn_id and str(turn_id) in compaction_turn_ids:
                    is_assistant_activity = False
                elif "last_agent_message" in payload and payload["last_agent_message"] is None:
                    is_assistant_activity = False
                else:
                    is_assistant_activity = True
            else:
                is_assistant_activity = False
        elif event_type == "response_item":
            ptype = payload.get("type")
            role = payload.get("role")
            if role == "assistant" or (
                role != "user" and ptype in ("message", "reasoning", "function_call", "custom_tool_call")
            ):
                is_assistant_activity = True
            else:
                is_assistant_activity = False
        elif event_type in ("assistant", "assistant_message"):
            is_assistant_activity = True
        elif event_type == "step":
            if record.get("source") in ("agent", "assistant"):
                is_assistant_activity = True
            else:
                is_assistant_activity = False
        elif event_type == "observation":
            is_assistant_activity = True
        else:
            is_assistant_activity = False

        if is_assistant_activity:
            completed_at = payload.get("completed_at")
            if isinstance(completed_at, (int, float)):
                try:
                    return datetime.fromtimestamp(completed_at, tz=timezone.utc)
                except (ValueError, OverflowError, OSError):
                    pass

            ts_str = record.get("timestamp") or payload.get("timestamp")
            if isinstance(ts_str, str):
                dt = parse_iso_timestamp(ts_str)
                if dt is not None:
                    return dt
    return None


def extract_context_tokens_from_jsonl(raw_text: str) -> int | None:
    """Extract prompt context token count from the most recent turn in a JSONL transcript."""
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

        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if payload.get("type") == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                last_usage = info.get("last_token_usage")
                if isinstance(last_usage, dict):
                    inp = last_usage.get("input_tokens")
                    if isinstance(inp, int) and inp > 0:
                        return inp
                    tot = last_usage.get("total_tokens")
                    if isinstance(tot, int) and tot > 0:
                        return tot
                total_usage = info.get("total_token_usage")
                if isinstance(total_usage, dict):
                    inp = total_usage.get("input_tokens")
                    if isinstance(inp, int) and inp > 0:
                        return inp

    return None


def get_agent_last_compacted_idle_since(agent: BaseAgent[Any]) -> datetime | None:
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


def record_agent_compacted(agent: BaseAgent[Any], idle_since: datetime | None = None) -> None:
    """Record that compaction was executed for the agent's current idle epoch."""
    agent_dir = agent._get_agent_dir()
    ts = idle_since or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    iso_str = ts.isoformat()
    agent.host.write_text_file(agent_dir / LAST_COMPACTED_IDLE_SINCE_FILENAME, iso_str)
    agent.host.write_text_file(agent_dir / IDLE_SINCE_FILENAME, iso_str)


def get_agent_idle_since(agent: BaseAgent[Any]) -> datetime | None:
    """Return the datetime when the agent entered idle state, or None if currently active/unknown."""
    try:
        if agent.get_lifecycle_state() == AgentLifecycleState.RUNNING:
            return None

        agent_dir = agent._get_agent_dir()
        idle_since_dt: datetime | None = None

        # Check transcript sources in order of preference
        transcript_candidates: list[Path] = [
            agent_dir / RAW_TRANSCRIPT_OUTPUT_RELATIVE,
        ]

        # Add rollout file path if recorded
        rollout_pointer = agent_dir / TRANSCRIPT_PATH_FILENAME
        if agent.host.path_exists(rollout_pointer):
            try:
                target_path = Path(agent.host.read_text_file(rollout_pointer).strip())
                transcript_candidates.append(target_path)
            except (OSError, ValueError):
                pass

        transcript_candidates.extend(
            [
                agent_dir / COMMON_TRANSCRIPT_OUTPUT_RELATIVE,
                agent_dir / "transcript.jsonl",
            ]
        )

        for path in transcript_candidates:
            if agent.host.path_exists(path):
                content = agent.host.read_text_file(path)
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
                "codex_process_started",
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


def get_agent_context_tokens(agent: BaseAgent[Any]) -> int | None:
    """Return the total prompt context token count from the agent's most recent turn, or None if unknown."""
    try:
        agent_dir = agent._get_agent_dir()
        candidates: list[Path] = [
            agent_dir / RAW_TRANSCRIPT_OUTPUT_RELATIVE,
        ]

        rollout_pointer = agent_dir / TRANSCRIPT_PATH_FILENAME
        if agent.host.path_exists(rollout_pointer):
            try:
                target_path = Path(agent.host.read_text_file(rollout_pointer).strip())
                candidates.append(target_path)
            except (OSError, ValueError):
                pass

        candidates.extend(
            [
                agent_dir / "events/codex/usage/events.jsonl",
                agent_dir / "transcript.jsonl",
            ]
        )

        for path in candidates:
            if agent.host.path_exists(path):
                content = agent.host.read_text_file(path)
                tokens = extract_context_tokens_from_jsonl(content)
                if tokens is not None:
                    return tokens
        return None
    except (MngrError, OSError, ValueError, KeyError, AttributeError) as e:
        logger.debug("Failed resolving context token count for agent {}: {}", agent.name, e)
        return None
