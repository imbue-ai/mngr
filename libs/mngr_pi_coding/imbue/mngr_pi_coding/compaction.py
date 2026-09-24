"""Context compaction helpers for the Pi coding agent."""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from loguru import logger

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentLifecycleState
from imbue.mngr_pi_coding.pi_coding_config import ACTIVE_MARKER_NAME
from imbue.mngr_pi_coding.pi_coding_config import COMMON_TRANSCRIPT_OUTPUT_RELATIVE
from imbue.mngr_pi_coding.pi_coding_config import IDLE_SINCE_FILENAME
from imbue.mngr_pi_coding.pi_coding_config import LAST_COMPACTED_IDLE_SINCE_FILENAME
from imbue.mngr_pi_coding.pi_coding_config import MODEL_STATE_FILENAME
from imbue.mngr_pi_coding.pi_coding_config import PI_DEFAULT_CACHE_TTL_MINUTES
from imbue.mngr_pi_coding.pi_coding_config import PI_OPENAI_CACHE_TTL_MINUTES
from imbue.mngr_pi_coding.pi_coding_config import RAW_TRANSCRIPT_OUTPUT_RELATIVE
from imbue.mngr_pi_coding.pi_coding_config import SESSION_POINTER_FILENAME
from imbue.mngr_pi_coding.pi_coding_config import USAGE_OUTPUT_RELATIVE


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
    """Extract timestamp of the most recent assistant turn or compaction in a JSONL transcript."""
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

        is_assistant_activity = False
        ts_val: Any = None

        event_type = record.get("type")
        msg = record.get("message")
        if isinstance(msg, dict):
            if msg.get("role") == "assistant":
                is_assistant_activity = True
                ts_val = record.get("timestamp") or msg.get("timestamp")
        elif event_type in ("assistant", "assistant_message"):
            is_assistant_activity = True
            ts_val = record.get("timestamp")
        elif event_type == "step":
            if record.get("source") in ("agent", "assistant"):
                is_assistant_activity = True
                ts_val = record.get("timestamp")
        elif event_type == "cost_snapshot":
            event_id = str(record.get("event_id", ""))
            if "compaction" not in event_id:
                is_assistant_activity = True
                ts_val = record.get("timestamp")
        else:
            pass

        if is_assistant_activity and ts_val is not None:
            if isinstance(ts_val, (int, float)):
                try:
                    # In Pi, timestamps in milliseconds (e.g. 1700000000000) vs seconds
                    if ts_val > 1e11:
                        ts_val = ts_val / 1000.0
                    return datetime.fromtimestamp(ts_val, tz=timezone.utc)
                except (ValueError, OverflowError, OSError):
                    pass
            elif isinstance(ts_val, str):
                dt = parse_iso_timestamp(ts_val)
                if dt is not None:
                    return dt
            else:
                pass

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

        # 1. Check raw assistant message usage (raw transcript or native session)
        msg = record.get("message")
        if isinstance(msg, dict):
            usage = msg.get("usage")
            if isinstance(usage, dict):
                inp = usage.get("input") or usage.get("input_tokens") or 0
                cached = usage.get("cacheRead") or usage.get("cache_read") or usage.get("cache_read_input_tokens") or 0
                cache_write = (
                    usage.get("cacheWrite")
                    or usage.get("cache_creation")
                    or usage.get("cache_creation_input_tokens")
                    or 0
                )
                if isinstance(inp, int) and isinstance(cached, int) and isinstance(cache_write, int):
                    total = inp + cached + cache_write
                    if total > 0:
                        return total
                total_tokens = usage.get("totalTokens") or usage.get("total_tokens")
                output = usage.get("output") or usage.get("output_tokens") or 0
                if isinstance(total_tokens, int) and isinstance(output, int) and total_tokens > output:
                    return total_tokens - output

        # 2. Check cost_snapshot tokens dict (usage stream)
        tokens = record.get("tokens")
        if isinstance(tokens, dict):
            inp = tokens.get("input") or 0
            cached = tokens.get("cache_read") or 0
            cache_creation = tokens.get("cache_creation") or 0
            if isinstance(inp, int) and isinstance(cached, int) and isinstance(cache_creation, int):
                total = inp + cached + cache_creation
                if total > 0:
                    return total

        # 3. Check common transcript metrics (ATIF step)
        metrics = record.get("metrics")
        if isinstance(metrics, dict):
            prompt_tokens = metrics.get("prompt_tokens")
            if isinstance(prompt_tokens, int) and prompt_tokens > 0:
                return prompt_tokens

        # 4. Check generic usage dict in record or payload
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        for candidate in (record.get("usage"), payload.get("usage")):
            if isinstance(candidate, dict):
                inp = candidate.get("input") or candidate.get("input_tokens") or 0
                cached = (
                    candidate.get("cacheRead")
                    or candidate.get("cache_read")
                    or candidate.get("cache_read_input_tokens")
                    or 0
                )
                cache_write = (
                    candidate.get("cacheWrite")
                    or candidate.get("cache_creation")
                    or candidate.get("cache_creation_input_tokens")
                    or 0
                )
                if isinstance(inp, int) and isinstance(cached, int) and isinstance(cache_write, int):
                    total = inp + cached + cache_write
                    if total > 0:
                        return total

        # 5. Check compaction entry tokensBefore
        if record.get("type") == "compaction":
            entry = record.get("entry") if isinstance(record.get("entry"), dict) else record
            tokens_before = entry.get("tokensBefore")
            if isinstance(tokens_before, int) and tokens_before > 0:
                return tokens_before

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
        if agent.host.path_exists(agent_dir / ACTIVE_MARKER_NAME):
            return None

        idle_since_dt: datetime | None = None

        # Check transcript sources in order of preference
        transcript_candidates: list[Path] = [
            agent_dir / COMMON_TRANSCRIPT_OUTPUT_RELATIVE,
            agent_dir / RAW_TRANSCRIPT_OUTPUT_RELATIVE,
            agent_dir / USAGE_OUTPUT_RELATIVE,
        ]

        # Add native session file if pointer recorded
        session_pointer = agent_dir / SESSION_POINTER_FILENAME
        if agent.host.path_exists(session_pointer):
            try:
                target_path = Path(agent.host.read_text_file(session_pointer).strip())
                transcript_candidates.append(target_path)
            except (OSError, ValueError):
                pass

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
                "pi_session_started",
                "pi_process_started",
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

        session_pointer = agent_dir / SESSION_POINTER_FILENAME
        if agent.host.path_exists(session_pointer):
            try:
                target_path = Path(agent.host.read_text_file(session_pointer).strip())
                candidates.append(target_path)
            except (OSError, ValueError):
                pass

        candidates.extend(
            [
                agent_dir / USAGE_OUTPUT_RELATIVE,
                agent_dir / COMMON_TRANSCRIPT_OUTPUT_RELATIVE,
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
        logger.debug("Failed resolving context tokens for agent {}: {}", agent.name, e)
        return None


def get_agent_cache_ttl_minutes(agent: BaseAgent[Any]) -> int | None:
    """Return the prompt cache TTL in minutes for the active model provider."""
    try:
        agent_dir = agent._get_agent_dir()
        model_state_path = agent_dir / MODEL_STATE_FILENAME
        if agent.host.path_exists(model_state_path):
            raw = agent.host.read_text_file(model_state_path)
            state = json.loads(raw)
            if isinstance(state, dict):
                model_str = state.get("model")
                if isinstance(model_str, str) and model_str.startswith("openai/"):
                    return PI_OPENAI_CACHE_TTL_MINUTES
    except (MngrError, OSError, ValueError, KeyError, AttributeError) as e:
        logger.debug("Failed reading model state for agent {}: {}", agent.name, e)

    return PI_DEFAULT_CACHE_TTL_MINUTES
