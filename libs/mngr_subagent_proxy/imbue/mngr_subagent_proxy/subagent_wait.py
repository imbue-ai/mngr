from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.mngr.config.host_dir import read_default_host_dir
from imbue.mngr_claude.claude_config import encode_claude_project_dir_name

_POLL_INTERVAL_SECONDS: Final[float] = 0.2
_SESSION_ID_RECHECK_SECONDS: Final[float] = 2.0
_HEARTBEAT_INTERVAL_SECONDS: Final[float] = 30.0
_END_TURN_SETTLE_SECONDS: Final[float] = 5.0
_TARGET_DISAPPEAR_GRACE_SECONDS: Final[float] = 10.0
_INITIAL_TARGET_WAIT_SECONDS: Final[float] = 60.0
_MNGR_LIST_TIMEOUT_SECONDS: Final[float] = 30.0
_DEFAULT_RESULT_MAX_CHARS: Final[int] = 200000
_RESULT_TRUNCATION_SUFFIX: Final[str] = "\n\n[truncated]"
_DESTROYED_PREFIX: Final[str] = "[mngr agent destroyed before completion] "


class SubagentWaitError(Exception):
    """Base error for subagent-wait failures."""


class TargetNotFoundError(SubagentWaitError):
    """Target mngr agent could not be found within the initial wait window."""


@dataclass
class _AgentLocation:
    """Paths for a located mngr agent, rooted in the local host dir."""

    host_dir: Path
    agent_id: str
    work_dir: Path

    @property
    def state_dir(self) -> Path:
        return self.host_dir / "agents" / self.agent_id

    @property
    def claude_projects_dir(self) -> Path:
        encoded = encode_claude_project_dir_name(self.work_dir)
        return self.state_dir / "plugin" / "claude" / "anthropic" / "projects" / encoded

    @property
    def session_id_file(self) -> Path:
        return self.state_dir / "claude_session_id"

    @property
    def permissions_waiting_file(self) -> Path:
        return self.state_dir / "permissions_waiting"

    @property
    def heartbeat_log(self) -> Path:
        return self.state_dir / "subagent_wait_heartbeat.log"


@dataclass
class _TailState:
    """Mutable state tracking a single JSONL transcript tail."""

    session_id: str | None = None
    path: Path | None = None
    offset: int = 0
    pending_buffer: str = ""
    text_events: list[dict] = field(default_factory=list)


def _run_mngr_list() -> list[dict]:
    """Invoke `uv run mngr list --format json` and return the parsed agents list."""
    try:
        completed = subprocess.run(
            ["uv", "run", "mngr", "list", "--format", "json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=_MNGR_LIST_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise SubagentWaitError(f"mngr list timed out after {_MNGR_LIST_TIMEOUT_SECONDS}s") from e
    except subprocess.CalledProcessError as e:
        raise SubagentWaitError(f"mngr list failed: {e.stderr}") from e

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as e:
        raise SubagentWaitError("mngr list returned invalid JSON") from e

    agents = payload.get("agents")
    if not isinstance(agents, list):
        raise SubagentWaitError("mngr list JSON missing 'agents' list")
    return agents


def _find_agent_by_name(agents: list[dict], target_name: str) -> dict | None:
    for agent in agents:
        if agent.get("name") == target_name:
            return agent
    return None


def _locate_target(target_name: str) -> _AgentLocation | None:
    """Look up the target agent via `mngr list` and return its paths, or None if missing."""
    agents = _run_mngr_list()
    agent = _find_agent_by_name(agents, target_name)
    if agent is None:
        return None

    agent_id = agent.get("id")
    work_dir_str = agent.get("work_dir")
    if not isinstance(agent_id, str) or not isinstance(work_dir_str, str):
        raise SubagentWaitError(f"mngr list entry for {target_name} missing id or work_dir")
    return _AgentLocation(
        host_dir=read_default_host_dir(),
        agent_id=agent_id,
        work_dir=Path(work_dir_str),
    )


def _wait_for_target(target_name: str, deadline: float) -> _AgentLocation:
    """Poll `mngr list` until the target appears or the deadline passes."""
    last_error: SubagentWaitError | None = None
    while time.monotonic() < deadline:
        try:
            location = _locate_target(target_name)
        except SubagentWaitError as e:
            last_error = e
            logger.warning("Transient mngr list failure while awaiting {}: {}", target_name, e)
            time.sleep(1.0)
            continue
        if location is not None:
            return location
        time.sleep(1.0)
    if last_error is not None:
        raise TargetNotFoundError(f"Target agent {target_name} never appeared (last error: {last_error})")
    raise TargetNotFoundError(f"Target agent {target_name} never appeared in mngr list")


def _read_session_id(location: _AgentLocation) -> str | None:
    """Read the current Claude session id from the atomic session file."""
    try:
        content = location.session_id_file.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Failed to read session id file {}: {}", location.session_id_file, e)
        return None
    session_id = content.strip()
    return session_id or None


def _read_new_jsonl_lines(state: _TailState) -> list[dict]:
    """Read any new lines appended to the tracked JSONL path since the last offset."""
    path = state.path
    if path is None:
        return []
    try:
        file_size = path.stat().st_size
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning("Failed to stat transcript {}: {}", path, e)
        return []

    # File truncated or replaced: reset the offset to avoid reading garbage.
    if file_size < state.offset:
        state.offset = 0
        state.pending_buffer = ""

    if file_size == state.offset:
        return []

    try:
        with path.open("rb") as handle:
            handle.seek(state.offset)
            chunk = handle.read(file_size - state.offset)
            state.offset = handle.tell()
    except OSError as e:
        logger.warning("Failed to read transcript {}: {}", path, e)
        return []

    combined = state.pending_buffer + chunk.decode("utf-8", errors="replace")
    lines = combined.split("\n")
    state.pending_buffer = lines[-1]
    completed_lines = lines[:-1]

    parsed: list[dict] = []
    for line in completed_lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as e:
            logger.warning("Malformed JSONL line in {}: {}", path, e)
            continue
        if isinstance(event, dict):
            parsed.append(event)
    return parsed


def _refresh_tail_path(state: _TailState, location: _AgentLocation) -> None:
    """Re-resolve the transcript path from the current session id, resetting offsets on change."""
    new_session_id = _read_session_id(location)
    if new_session_id is None:
        return
    if new_session_id == state.session_id and state.path is not None:
        return
    new_path = location.claude_projects_dir / f"{new_session_id}.jsonl"
    if state.session_id is not None and new_session_id != state.session_id:
        logger.info("Session id changed ({} -> {}); resetting transcript tail", state.session_id, new_session_id)
    state.session_id = new_session_id
    state.path = new_path
    state.offset = 0
    state.pending_buffer = ""


def _is_end_turn_event(event: dict) -> bool:
    """Return True for an assistant stop_reason=end_turn message with no tool_use blocks."""
    if event.get("type") != "assistant":
        return False
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    if message.get("stop_reason") != "end_turn":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return False
    return True


def _extract_assistant_text(event: dict) -> str:
    """Concatenate text blocks from an assistant message event."""
    message = event.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def _is_user_event(event: dict) -> bool:
    return event.get("type") == "user"


def _truncate_result_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    budget = max(max_chars - len(_RESULT_TRUNCATION_SUFFIX), 0)
    return text[:budget] + _RESULT_TRUNCATION_SUFFIX


def _get_result_max_chars() -> int:
    raw = os.environ.get("MNGR_SUBAGENT_RESULT_MAX_CHARS")
    if raw is None:
        return _DEFAULT_RESULT_MAX_CHARS
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid MNGR_SUBAGENT_RESULT_MAX_CHARS={!r}; using default", raw)
        return _DEFAULT_RESULT_MAX_CHARS
    if value <= 0:
        return _DEFAULT_RESULT_MAX_CHARS
    return value


def _write_heartbeat(location: _AgentLocation, now: float) -> None:
    try:
        location.heartbeat_log.parent.mkdir(parents=True, exist_ok=True)
        with location.heartbeat_log.open("a") as handle:
            handle.write(f"heartbeat: {now}\n")
    except OSError as e:
        logger.warning("Failed to write heartbeat: {}", e)


@dataclass
class _WaitRuntime:
    """Mutable loop state for the polling wait."""

    target_name: str
    location: _AgentLocation
    permissions_previously_waiting: bool
    tail_state: _TailState = field(default_factory=_TailState)
    pending_end_turn_text: str | None = None
    pending_end_turn_deadline: float | None = None
    last_heartbeat_at: float = 0.0
    last_session_id_check_at: float = 0.0
    target_missing_since: float | None = None


def _process_new_events(
    runtime: _WaitRuntime,
    events: list[dict],
    now: float,
) -> None:
    """Update pending end-turn state based on newly observed transcript events."""
    for event in events:
        if _is_end_turn_event(event):
            runtime.pending_end_turn_text = _extract_assistant_text(event)
            runtime.pending_end_turn_deadline = now + _END_TURN_SETTLE_SECONDS
        elif _is_user_event(event) and runtime.pending_end_turn_text is not None:
            logger.info("New user event during settle window; discarding pending end_turn")
            runtime.pending_end_turn_text = None
            runtime.pending_end_turn_deadline = None


def _check_permissions_newly_waiting(runtime: _WaitRuntime) -> bool:
    """Return True if permissions_waiting file newly appeared since the last poll."""
    is_waiting_now = runtime.location.permissions_waiting_file.exists()
    if is_waiting_now and not runtime.permissions_previously_waiting:
        runtime.permissions_previously_waiting = True
        return True
    runtime.permissions_previously_waiting = is_waiting_now
    return False


def _check_target_still_present(runtime: _WaitRuntime, now: float) -> bool:
    """Return True if the target has been missing from `mngr list` beyond the grace window."""
    try:
        agents = _run_mngr_list()
    except SubagentWaitError as e:
        logger.warning("mngr list failed during tail of {}: {}", runtime.target_name, e)
        return False
    agent = _find_agent_by_name(agents, runtime.target_name)
    if agent is None:
        if runtime.target_missing_since is None:
            runtime.target_missing_since = now
        elapsed = now - runtime.target_missing_since
        return elapsed > _TARGET_DISAPPEAR_GRACE_SECONDS
    runtime.target_missing_since = None
    return False


def _resolve_destroyed_result(target_name: str, location: _AgentLocation) -> str:
    """Build the END_TURN payload for an agent that was destroyed before completing."""
    preserved_root = location.host_dir / "plugin" / "mngr_claude" / "preserved_sessions"
    events_path = preserved_root / f"{target_name}--{location.agent_id}" / "common_transcript" / "events.jsonl"
    last_text = ""
    if events_path.exists():
        try:
            with events_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") != "assistant_message":
                        continue
                    text = event.get("text")
                    if isinstance(text, str) and text:
                        last_text = text
        except OSError as e:
            logger.warning("Failed to read preserved events {}: {}", events_path, e)
    return f"{_DESTROYED_PREFIX}{last_text}"


def wait_for_subagent(target_name: str) -> str:
    """Block until the target mngr agent reaches end_turn, requests permission, or is destroyed."""
    deadline = time.monotonic() + _INITIAL_TARGET_WAIT_SECONDS
    location = _wait_for_target(target_name, deadline)

    runtime = _WaitRuntime(
        target_name=target_name,
        location=location,
        permissions_previously_waiting=location.permissions_waiting_file.exists(),
    )
    max_chars = _get_result_max_chars()

    while True:
        now = time.monotonic()

        if now - runtime.last_heartbeat_at >= _HEARTBEAT_INTERVAL_SECONDS:
            _write_heartbeat(location, now)
            runtime.last_heartbeat_at = now

        if now - runtime.last_session_id_check_at >= _SESSION_ID_RECHECK_SECONDS:
            _refresh_tail_path(runtime.tail_state, location)
            runtime.last_session_id_check_at = now

        new_events = _read_new_jsonl_lines(runtime.tail_state)
        if new_events:
            _process_new_events(runtime, new_events, now)

        if _check_permissions_newly_waiting(runtime):
            return f"PERMISSION_REQUIRED:{target_name}"

        if (
            runtime.pending_end_turn_text is not None
            and runtime.pending_end_turn_deadline is not None
            and now >= runtime.pending_end_turn_deadline
        ):
            truncated = _truncate_result_text(runtime.pending_end_turn_text, max_chars)
            return f"END_TURN:{truncated}"

        if _check_target_still_present(runtime, now):
            destroyed_text = _resolve_destroyed_result(target_name, location)
            truncated = _truncate_result_text(destroyed_text, max_chars)
            return f"END_TURN:{truncated}"

        time.sleep(_POLL_INTERVAL_SECONDS)


def main() -> None:
    if len(sys.argv) != 2:
        logger.error("Usage: python -m imbue.mngr_subagent_proxy.subagent_wait <target_name>")
        sys.exit(2)
    target_name = sys.argv[1]
    try:
        result = wait_for_subagent(target_name)
    except TargetNotFoundError as e:
        logger.error("Target not found: {}", e)
        sys.exit(2)
    except SubagentWaitError as e:
        logger.error("Subagent wait failed: {}", e)
        sys.exit(2)
    sys.stdout.write(result)
    sys.stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    main()
