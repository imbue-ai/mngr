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
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr_claude.claude_config import encode_claude_project_dir_name
from imbue.mngr_claude.plugin import get_preserved_sessions_dir_for_host
from imbue.mngr_subagent_proxy.mngr_binary import get_mngr_command

_POLL_INTERVAL_SECONDS: Final[float] = 0.2
_SESSION_ID_RECHECK_SECONDS: Final[float] = 2.0
_HEARTBEAT_INTERVAL_SECONDS: Final[float] = 30.0
_END_TURN_SETTLE_SECONDS: Final[float] = 5.0
_TARGET_DISAPPEAR_GRACE_SECONDS: Final[float] = 10.0
_INITIAL_TARGET_WAIT_SECONDS: Final[float] = 60.0
_MNGR_LIST_TIMEOUT_SECONDS: Final[float] = 30.0
# `mngr list` is expensive (multi-second on hosts with 30+ agents), so we
# do not invoke it on every poll. Each call from the main wait loop is
# rate-limited to this interval. Without this, the loop fires 5x/s and
# can saturate the host -- observed live: nested verify-and-fix subagent
# wedged when mngr list timed out repeatedly because the wait loop kept
# re-issuing it before previous calls finished.
_TARGET_PRESENCE_RECHECK_SECONDS: Final[float] = 5.0
# ~100KB roughly matches Claude Code's native Task tool_result truncation
# threshold (~25k tokens, observed empirically). Override via
# MNGR_SUBAGENT_RESULT_MAX_CHARS if your subagents legitimately produce more.
_DEFAULT_RESULT_MAX_CHARS: Final[int] = 100000
_RESULT_TRUNCATION_SUFFIX: Final[str] = "\n\n[truncated]"

# Prefix the body of any non-success END_TURN result so the parent agent
# (which sees the body as its tool_result via Haiku's echo) recognizes
# this as an error rather than a normal subagent reply. Native Claude
# Code uses tool_result.is_error: true for this; we can't set that flag
# from inside Haiku's assistant turn, so we ride a textual prefix.
ERROR_PREFIX: Final[str] = "[ERROR] "
_DESTROYED_PREFIX: Final[str] = ERROR_PREFIX + "mngr subagent destroyed before completion: "


class SubagentWaitError(Exception):
    """Base error for subagent-wait failures."""


class TargetNotFoundError(SubagentWaitError):
    """Target mngr agent could not be found within the initial wait window."""


@dataclass
class AgentLocation:
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
class TailState:
    """Mutable state tracking a single JSONL transcript tail."""

    session_id: str | None = None
    path: Path | None = None
    offset: int = 0
    pending_buffer: str = ""


def _run_mngr_list() -> list[dict]:
    """Invoke `mngr list --format json` and return the parsed agents list.

    Uses the per-agent mngr binary when mngr_recursive has provisioned one
    (``$UV_TOOL_BIN_DIR/mngr``); falls back to ``uv run mngr`` otherwise.
    """
    try:
        completed = subprocess.run(
            get_mngr_command() + ["list", "--format", "json"],
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


def _locate_target(target_name: str) -> AgentLocation | None:
    """Look up the target agent via `mngr list` and return its paths, or None if missing."""
    agents = _run_mngr_list()
    agent = _find_agent_by_name(agents, target_name)
    if agent is None:
        return None

    agent_id = agent.get("id")
    work_dir_str = agent.get("work_dir")
    if not isinstance(agent_id, str) or not isinstance(work_dir_str, str):
        raise SubagentWaitError(f"mngr list entry for {target_name} missing id or work_dir")
    return AgentLocation(
        host_dir=read_default_host_dir(),
        agent_id=agent_id,
        work_dir=Path(work_dir_str),
    )


def _wait_for_target(target_name: str, deadline: float) -> AgentLocation:
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


def _read_session_id(location: AgentLocation) -> str | None:
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


def read_new_jsonl_lines(state: TailState) -> list[dict]:
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


def _refresh_tail_path(state: TailState, location: AgentLocation) -> None:
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


# Stop reasons that mean "the assistant is finished talking and isn't
# calling a tool." Any of these on an assistant message with no tool_use
# blocks is a real end-of-turn from our perspective.
#
# - end_turn: normal completion.
# - stop_sequence: model hit a configured stop sequence (Claude Code
#   sometimes uses these for skill / agent integrations); the assistant
#   is done with no tool call.
# - max_tokens: model truncated; treat as done so we surface what we
#   have rather than hanging forever.
_TERMINAL_STOP_REASONS: Final[frozenset[str]] = frozenset({"end_turn", "stop_sequence", "max_tokens"})


def is_end_turn_event(event: dict) -> bool:
    """Return True for an assistant message that finishes the turn without a tool call."""
    if event.get("type") != "assistant":
        return False
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    if message.get("stop_reason") not in _TERMINAL_STOP_REASONS:
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return False
    return True


def extract_assistant_text(event: dict) -> str:
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


_MACHINE_USER_PREFIXES: Final[tuple[str, ...]] = (
    "Stop hook feedback:",
    "PostToolUse hook feedback:",
    "PreToolUse hook feedback:",
    "PostToolUseFailure hook feedback:",
    "UserPromptSubmit hook feedback:",
    "Notification hook feedback:",
    "SessionStart hook feedback:",
)


def is_real_user_event(event: dict) -> bool:
    """Return True only for events that look like a fresh human-typed prompt.

    Claude Code emits ``type=user`` for three distinct things:
    (1) actual user prompts (human input), (2) ``tool_result`` blocks echoed
    back to the assistant, and (3) synthetic hook-injected messages
    ("Stop hook feedback: ..."). Only (1) should reset the end-turn settle
    window; (2) and (3) are machinery that fires during and after a normal
    assistant turn and would otherwise prevent the settle from ever
    elapsing.
    """
    if event.get("type") != "user":
        return False
    message = event.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, list):
        return False
    if isinstance(content, str):
        stripped = content.lstrip()
        for prefix in _MACHINE_USER_PREFIXES:
            if stripped.startswith(prefix):
                return False
        return True
    return False


def truncate_result_text(text: str, max_chars: int) -> str:
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
        logger.warning(
            "Non-positive MNGR_SUBAGENT_RESULT_MAX_CHARS={}; using default {}",
            value,
            _DEFAULT_RESULT_MAX_CHARS,
        )
        return _DEFAULT_RESULT_MAX_CHARS
    return value


def _write_heartbeat(location: AgentLocation, now: float) -> None:
    """Emit a heartbeat to both stderr and the side log.

    The stderr write is what actually keeps Bash's idle-output timeout
    happy: file writes don't count, but stderr does. The side-log entry
    is for human diagnostics. Stdout is reserved for the final
    END_TURN payload that Haiku echoes -- never write heartbeat noise
    there.
    """
    try:
        sys.stderr.write(f"mngr_subagent_proxy.heartbeat: {now}\n")
        sys.stderr.flush()
    except OSError as e:
        logger.warning("Failed to write stderr heartbeat: {}", e)
    try:
        location.heartbeat_log.parent.mkdir(parents=True, exist_ok=True)
        with location.heartbeat_log.open("a") as handle:
            handle.write(f"heartbeat: {now}\n")
    except OSError as e:
        logger.warning("Failed to write heartbeat side log: {}", e)


@dataclass
class _WaitRuntime:
    """Mutable loop state for the polling wait."""

    target_name: str
    location: AgentLocation
    permissions_previously_waiting: bool
    # Byte-watermark on the target's transcript: while the file size is
    # at-or-below this value, ignore permission_waiting transitions. The
    # caller sets this on re-entry after surfacing PERMISSION_REQUIRED to
    # the human, so we don't re-fire on the SAME pending dialog. Once the
    # target writes any new bytes past the watermark (i.e. the dialog has
    # been resolved and the agent has progressed), the gate lifts and any
    # subsequent flag transition is a genuinely new permission event.
    permission_gate_until_transcript_past: int = 0
    tail_state: TailState = field(default_factory=TailState)
    pending_end_turn_text: str | None = None
    # True when the pending end-turn was an API error (e.g. rate-limit /
    # transient API failure). Claude Code records these as
    # ``isApiErrorMessage: true`` on an assistant message that still
    # carries a terminal stop_reason -- so our normal end-turn detection
    # picks them up, and we need to flag them at emission time so the
    # parent's tool_result is prefixed with [ERROR]. Without this the
    # parent sees a literal "API Error: ..." string but no signal it's
    # an error, and treats the body as a legitimate subagent reply --
    # which silently wedges the chain (every level above echoes the
    # error text as success and ends its turn).
    pending_end_turn_is_api_error: bool = False
    pending_end_turn_deadline: float | None = None
    last_heartbeat_at: float = 0.0
    last_session_id_check_at: float = 0.0
    last_presence_check_at: float = 0.0
    target_missing_since: float | None = None


def is_api_error_event(event: dict) -> bool:
    """Return True if the event is an assistant message Claude Code marked
    as an API error (e.g. rate limit, transient API failure).

    Claude Code emits ``isApiErrorMessage: true`` on the assistant event
    body for these. The event still carries a terminal stop_reason and
    no tool_use, so it passes ``is_end_turn_event`` -- but we need a
    separate signal to tag the relayed body with our [ERROR] prefix.
    """
    if event.get("type") != "assistant":
        return False
    return bool(event.get("isApiErrorMessage"))


def _process_new_events(
    runtime: _WaitRuntime,
    events: list[dict],
    now: float,
) -> None:
    """Update pending end-turn state based on newly observed transcript events."""
    for event in events:
        if is_end_turn_event(event):
            runtime.pending_end_turn_text = extract_assistant_text(event)
            runtime.pending_end_turn_is_api_error = is_api_error_event(event)
            runtime.pending_end_turn_deadline = now + _END_TURN_SETTLE_SECONDS
        elif is_real_user_event(event) and runtime.pending_end_turn_text is not None:
            logger.info("New user event during settle window; discarding pending end_turn")
            runtime.pending_end_turn_text = None
            runtime.pending_end_turn_is_api_error = False
            runtime.pending_end_turn_deadline = None


def _current_transcript_size(runtime: _WaitRuntime) -> int:
    """Return the current size of the target's transcript file in bytes, or 0 if unavailable."""
    path = runtime.tail_state.path
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _check_permissions_newly_waiting(runtime: _WaitRuntime) -> bool:
    """Return True if permissions_waiting file newly appeared AND the
    permission gate (transcript watermark) has lifted.

    The gate is set by the caller on re-entry to suppress the same
    pending dialog from re-firing. It lifts as soon as the target has
    written any new transcript bytes past the watermark -- which only
    happens after the original dialog resolves and the agent moves on.
    """
    is_waiting_now = runtime.location.permissions_waiting_file.exists()
    if is_waiting_now and not runtime.permissions_previously_waiting:
        if _current_transcript_size(runtime) <= runtime.permission_gate_until_transcript_past:
            # Same dialog (or one fired before the agent has moved past
            # the prior watermark). Track the new "is_waiting" state but
            # don't surface yet -- caller already relayed this one.
            runtime.permissions_previously_waiting = True
            return False
        runtime.permissions_previously_waiting = True
        return True
    runtime.permissions_previously_waiting = is_waiting_now
    return False


def _has_target_disappeared_past_grace(runtime: _WaitRuntime, now: float) -> bool:
    """Return True if the target has been missing from `mngr list` beyond the grace window.

    On a transient mngr-list error the grace clock is reset rather than left
    ticking: the elapsed-since-missing window must only count time during
    which the target was confirmed absent in a successful listing. Otherwise
    a long mngr-list outage between an initial "missing" observation and the
    next successful call could falsely cross the grace threshold.
    """
    try:
        agents = _run_mngr_list()
    except SubagentWaitError as e:
        logger.warning("mngr list failed during tail of {}: {}", runtime.target_name, e)
        runtime.target_missing_since = None
        return False
    agent = _find_agent_by_name(agents, runtime.target_name)
    if agent is None:
        if runtime.target_missing_since is None:
            runtime.target_missing_since = now
        elapsed = now - runtime.target_missing_since
        return elapsed > _TARGET_DISAPPEAR_GRACE_SECONDS
    runtime.target_missing_since = None
    return False


def resolve_destroyed_result(target_name: str, location: AgentLocation) -> str:
    """Build the END_TURN payload for an agent that was destroyed before completing."""
    preserved_dir = get_preserved_sessions_dir_for_host(
        location.host_dir, AgentName(target_name), AgentId(location.agent_id)
    )
    events_path = preserved_dir / "common_transcript" / "events.jsonl"
    last_text = ""
    if events_path.exists():
        try:
            # Materialize so we can distinguish "final line" (race-prone:
            # may be a partial write captured during preservation finalize)
            # from earlier lines (a malformed earlier line is a real anomaly).
            raw_lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for idx, line in enumerate(raw_lines):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError as e:
                    is_final = idx == len(raw_lines) - 1
                    if is_final:
                        # Race with preserve-finalize: last line may be a
                        # mid-write partial. Don't surface as a warning.
                        logger.trace("Skipping malformed final line in {}: {}", events_path, e)
                    else:
                        logger.warning("Malformed JSONL line in {}: {}", events_path, e)
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


def _read_watermark_file(path: Path) -> int:
    """Read an integer watermark from a sidefile; return 0 on missing/invalid."""
    try:
        content = path.read_text().strip()
    except FileNotFoundError:
        return 0
    except OSError as e:
        logger.warning("Failed to read watermark file {}: {}", path, e)
        return 0
    try:
        return int(content)
    except ValueError:
        logger.warning("Invalid watermark file content {!r} in {}; ignoring", content, path)
        return 0


def _write_watermark_file(path: Path, value: int) -> None:
    """Write an integer watermark to a sidefile (best-effort)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value))
    except OSError as e:
        logger.warning("Failed to write watermark file {}: {}", path, e)


def _delete_watermark_file(path: Path) -> None:
    """Delete a watermark sidefile (best-effort, no-op if absent)."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning("Failed to delete watermark file {}: {}", path, e)


def wait_for_subagent(target_name: str, watermark_file: Path | None = None) -> str:
    """Block until the target mngr agent reaches end_turn, requests permission, or is destroyed.

    ``watermark_file`` (optional) is a sidefile owned entirely by this
    module. On startup, it's read for the initial transcript watermark
    that suppresses re-firing of a previously-surfaced permission
    dialog. On PERMISSION_REQUIRED return it's overwritten with the
    current transcript byte-size. On terminal return (END_TURN) it's
    deleted. Callers (the wait-script) just point this at a stable
    per-tool_use_id path; they don't need to interpret or carry state.
    """
    deadline = time.monotonic() + _INITIAL_TARGET_WAIT_SECONDS
    location = _wait_for_target(target_name, deadline)

    initial_watermark = _read_watermark_file(watermark_file) if watermark_file is not None else 0

    runtime = _WaitRuntime(
        target_name=target_name,
        location=location,
        permissions_previously_waiting=location.permissions_waiting_file.exists(),
        permission_gate_until_transcript_past=initial_watermark,
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

        new_events = read_new_jsonl_lines(runtime.tail_state)
        if new_events:
            _process_new_events(runtime, new_events, now)

        if _check_permissions_newly_waiting(runtime):
            current_bytes = _current_transcript_size(runtime)
            if watermark_file is not None:
                _write_watermark_file(watermark_file, current_bytes)
            return f"PERMISSION_REQUIRED:{target_name}"

        if (
            runtime.pending_end_turn_text is not None
            and runtime.pending_end_turn_deadline is not None
            and now >= runtime.pending_end_turn_deadline
        ):
            body = runtime.pending_end_turn_text
            if runtime.pending_end_turn_is_api_error:
                # Tag API errors so parent treats them as error tool_result.
                # Claude Code's tool_result.is_error is unreachable from
                # inside Haiku's reply, so we ride the textual prefix the
                # rest of the codebase already keys off of.
                body = ERROR_PREFIX + body
            truncated = truncate_result_text(body, max_chars)
            if watermark_file is not None:
                _delete_watermark_file(watermark_file)
            return f"END_TURN:{truncated}"

        if now - runtime.last_presence_check_at >= _TARGET_PRESENCE_RECHECK_SECONDS:
            runtime.last_presence_check_at = now
            if _has_target_disappeared_past_grace(runtime, now):
                destroyed_text = resolve_destroyed_result(target_name, location)
                truncated = truncate_result_text(destroyed_text, max_chars)
                if watermark_file is not None:
                    _delete_watermark_file(watermark_file)
                return f"END_TURN:{truncated}"

        time.sleep(_POLL_INTERVAL_SECONDS)


_USAGE: Final[str] = "Usage: python -m imbue.mngr_subagent_proxy.subagent_wait <target_name> [--watermark-file <path>]"


def _parse_args(argv: list[str]) -> tuple[str, Path | None]:
    """Parse the script's CLI args. Returns (target_name, watermark_file)."""
    if len(argv) == 2:
        return argv[1], None
    if len(argv) == 4 and argv[2] == "--watermark-file":
        return argv[1], Path(argv[3])
    logger.error(_USAGE)
    sys.exit(2)


def main() -> None:
    target_name, watermark_file = _parse_args(sys.argv)
    try:
        result = wait_for_subagent(target_name, watermark_file=watermark_file)
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
