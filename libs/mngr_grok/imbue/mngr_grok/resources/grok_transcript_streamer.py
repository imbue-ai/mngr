#!/usr/bin/env python3
"""Raw-transcript streamer for grok agents, run by ``stream_transcript.sh``.

Tails the ``updates.jsonl`` of every root session listed in ``plugin/grok/roots`` and
of every child session a ``subagent_spawned`` record names (children live in their
parent's bucket under ``plugin/grok/home/sessions``), and appends each new complete
line to ``logs/grok_transcript/events.jsonl`` as::

    {"session_id": "<sid>", "parent_session_id": "<parent sid>" | null,
     "offset": <byte offset of the line in its updates.jsonl>, "record": <the native line>}

The native line is embedded as written, without its surrounding whitespace; a line that
is not valid JSON is carried as ``"unparsed": "<text>"`` instead. Appends run under the
per-agent convert lock that ``mngr_common_transcript_lib.sh`` implements, and the
per-file offsets and the discovered child sessions persist in
``plugin/grok/streamer_state.json``.

After a pass that appended a root session's ``turn_completed``, the streamer releases
the lock, runs ``commands/common_transcript.sh --single-pass --hold-open-steps``, then
calls ``grok_hook.py --turn-completed <session id> <prompt id>``.
Each failed, unblocked ``global/mngr:`` hook run is recorded in
``events/grok/hook_failures/events.jsonl``. Records copied from another session (a fork
replays its source's history) trigger none of these.

Usage: grok_transcript_streamer.py [--single-pass]
Standard library only, compatible with Python 3.9.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final
from typing import Optional

POLL_INTERVAL_SECONDS: Final = 1.0
DEFAULT_LOCK_TIMEOUT_SECONDS: Final = 30.0
_LOCK_RETRY_INTERVAL_SECONDS: Final = 0.1
# Matches the `find -mmin +1` stale break in mngr_common_transcript_lib.sh.
_STALE_LOCK_AGE_SECONDS: Final = 60.0
_FOLLOW_UP_TIMEOUT_SECONDS: Final = 120.0
_MNGR_HOOK_RUN_PREFIX: Final = "global/mngr:"
_SESSION_ID_PATTERN: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_TERMINATION_SIGNALS: Final = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
_LOG_SOURCE: Final = "logs/stream_transcript"
_LOG_TYPE: Final = "stream_transcript"
_HOOK_FAILURE_SOURCE: Final = "grok/hook_failures"
_LOGGER: Final = logging.getLogger("grok_transcript_streamer")


def _grok_plugin_dir(state_dir: Path) -> Path:
    return state_dir / "plugin" / "grok"


def sessions_dir_path(state_dir: Path) -> Path:
    return _grok_plugin_dir(state_dir) / "home" / "sessions"


def _hook_handler_path(state_dir: Path) -> Path:
    return _grok_plugin_dir(state_dir) / "home" / "hooks" / "grok_hook.py"


def streamer_state_path(state_dir: Path) -> Path:
    return _grok_plugin_dir(state_dir) / "streamer_state.json"


def _raw_transcript_path(state_dir: Path) -> Path:
    return state_dir / "logs" / "grok_transcript" / "events.jsonl"


def _hook_failure_events_path(state_dir: Path) -> Path:
    return state_dir / "events" / "grok" / "hook_failures" / "events.jsonl"


def convert_lock_path(state_dir: Path) -> Path:
    return state_dir / ".common_transcript_convert.lock"


def _is_valid_session_id(session_id: object) -> bool:
    return isinstance(session_id, str) and _SESSION_ID_PATTERN.match(session_id) is not None


def format_event_timestamp(epoch_seconds: float) -> str:
    """Format a UTC time with nanosecond-width precision, e.g. ``2026-10-20T01:14:10.029000000Z``."""
    whole_seconds = int(epoch_seconds)
    microseconds = min(999_999, int(round((epoch_seconds - whole_seconds) * 1_000_000)))
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(whole_seconds)) + ".{:06d}000Z".format(microseconds)


class _JsonlLogFormatter(logging.Formatter):
    """Formats log records as the JSONL lines ``mngr_log.sh`` writes."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": format_event_timestamp(record.created),
                "type": _LOG_TYPE,
                "event_id": "evt-" + uuid.uuid4().hex,
                "source": _LOG_SOURCE,
                "level": record.levelname,
                "message": record.getMessage(),
                "pid": os.getpid(),
            }
        )


def configure_logging(state_dir: Path) -> None:
    log_path = state_dir / "events" / "logs" / "stream_transcript" / "events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(str(log_path), encoding="utf-8")
    handler.setFormatter(_JsonlLogFormatter())
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False


def read_lock_timeout_seconds(environ: Mapping[str, str]) -> float:
    raw_value = environ.get("MNGR_CONVERT_LOCK_TIMEOUT", "")
    if not raw_value:
        return DEFAULT_LOCK_TIMEOUT_SECONDS
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        _LOGGER.warning("Ignored non-numeric MNGR_CONVERT_LOCK_TIMEOUT=%r", raw_value)
        return DEFAULT_LOCK_TIMEOUT_SECONDS


def is_termination_pending() -> bool:
    pending = signal.sigpending()
    return any(termination_signal in pending for termination_signal in _TERMINATION_SIGNALS)


@contextmanager
def termination_signals_deferred() -> Generator[None, None, None]:
    """Hold SIGTERM, SIGINT and SIGHUP until the block exits, so a stop never lands mid-append."""
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _TERMINATION_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _try_make_lock_dir(lock_path: Path) -> bool:
    try:
        lock_path.mkdir()
    except FileExistsError:
        return False
    return True


def _is_lock_stale(lock_path: Path) -> bool:
    try:
        modified_at = lock_path.stat().st_mtime
    except FileNotFoundError:
        return False
    return time.time() - modified_at > _STALE_LOCK_AGE_SECONDS


def release_convert_lock(lock_path: Path) -> None:
    try:
        lock_path.rmdir()
    except FileNotFoundError:
        pass


def acquire_convert_lock(lock_path: Path, timeout_seconds: float) -> bool:
    """Take the convert lock within ``timeout_seconds``; gives up early when a stop signal is pending."""
    deadline = time.monotonic() + timeout_seconds
    while not _try_make_lock_dir(lock_path):
        if _is_lock_stale(lock_path):
            _LOGGER.warning("Broke a stale convert lock at %s", lock_path)
            release_convert_lock(lock_path)
            continue
        if time.monotonic() >= deadline or is_termination_pending():
            return False
        time.sleep(_LOCK_RETRY_INTERVAL_SECONDS)
    return True


def read_root_session_ids(roots_path: Path) -> list[str]:
    if not roots_path.is_file():
        return []
    session_ids: list[str] = []
    for line in roots_path.read_text(encoding="utf-8", errors="replace").splitlines():
        session_id = line.strip()
        if _is_valid_session_id(session_id) and session_id not in session_ids:
            session_ids.append(session_id)
    return session_ids


def _find_session_file_keys(sessions_dir: Path, session_id: str) -> list[str]:
    """Return ``<bucket>/<session id>/updates.jsonl`` for every bucket holding the session."""
    if not sessions_dir.is_dir():
        return []
    return [
        "{}/{}/updates.jsonl".format(bucket, session_id)
        for bucket in sorted(os.listdir(str(sessions_dir)))
        if (sessions_dir / bucket / session_id / "updates.jsonl").is_file()
    ]


def new_streamer_state() -> dict[str, Any]:
    return {"files": {}}


def _is_valid_file_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    offset = entry.get("offset")
    parent_session_id = entry.get("parent_session_id")
    return (
        isinstance(offset, int)
        and not isinstance(offset, bool)
        and offset >= 0
        and _is_valid_session_id(entry.get("session_id"))
        and (parent_session_id is None or _is_valid_session_id(parent_session_id))
    )


def load_streamer_state(state_path: Path) -> dict[str, Any]:
    """The saved streamer state; an unreadable or wrongly shaped state starts over, and a malformed entry is dropped."""
    if not state_path.is_file():
        return new_streamer_state()
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        _LOGGER.warning("Reset unreadable streamer state %s: %s", state_path, error)
        return new_streamer_state()
    if not isinstance(loaded, dict) or not isinstance(loaded.get("files"), dict):
        _LOGGER.warning("Reset streamer state %s with an unexpected shape", state_path)
        return new_streamer_state()
    malformed_file_keys = sorted(key for key, entry in loaded["files"].items() if not _is_valid_file_entry(entry))
    if malformed_file_keys:
        _LOGGER.warning("Dropped malformed streamer state entries %s from %s", malformed_file_keys, state_path)
    return {"files": {key: entry for key, entry in loaded["files"].items() if key not in malformed_file_keys}}


def _save_streamer_state(state_path: Path, state: Mapping[str, Any]) -> None:
    temporary_path = state_path.with_name(state_path.name + ".tmp")
    temporary_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(str(temporary_path), str(state_path))


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _record_update(record: Mapping[str, Any]) -> dict[str, Any]:
    params = record.get("params")
    update = params.get("update") if isinstance(params, dict) else None
    return update if isinstance(update, dict) else {}


def _record_meta(record: Mapping[str, Any]) -> dict[str, Any]:
    params = record.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return meta if isinstance(meta, dict) else {}


def is_copied_record(record: Mapping[str, Any], session_id: str) -> bool:
    """Whether a record was written by another session (``eventId`` is ``<writing session id>-<n>``)."""
    event_id = _record_meta(record).get("eventId")
    return isinstance(event_id, str) and not event_id.startswith(session_id + "-")


def record_epoch_seconds(record: Mapping[str, Any]) -> float:
    timestamp_ms = _record_meta(record).get("agentTimestampMs")
    if isinstance(timestamp_ms, (int, float)) and not isinstance(timestamp_ms, bool):
        return timestamp_ms / 1000.0
    timestamp = record.get("timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        return float(timestamp)
    return time.time()


def _build_tagged_line(
    session_id: str, parent_session_id: Optional[str], offset: int, line_bytes: bytes
) -> tuple[bytes, Optional[dict[str, Any]]]:
    """Return the raw-transcript line for one native line, and the native record when it is a JSON object."""
    prefix = '{{"session_id":{},"parent_session_id":{},"offset":{},'.format(
        json.dumps(session_id), json.dumps(parent_session_id), offset
    ).encode("utf-8")
    try:
        parsed = json.loads(line_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        _LOGGER.warning("Carried an unparseable line of session %s at offset %d: %s", session_id, offset, error)
        unparsed = json.dumps(line_bytes.decode("utf-8", errors="replace")).encode("utf-8")
        return prefix + b'"unparsed":' + unparsed + b"}\n", None
    record = parsed if isinstance(parsed, dict) else None
    return prefix + b'"record":' + line_bytes.strip() + b"}\n", record


def new_follow_ups() -> dict[str, Any]:
    return {"turn_completions": [], "hook_failure_events": []}


def _build_hook_failure_event(
    record: Mapping[str, Any],
    session_id: str,
    parent_session_id: Optional[str],
    offset: int,
    run_index: int,
    run_name: str,
    error: object,
) -> dict[str, Any]:
    update = _record_update(record)
    digest = hashlib.sha256("{}:{}:{}".format(session_id, offset, run_index).encode("utf-8")).hexdigest()[:32]
    event: dict[str, Any] = {
        "timestamp": format_event_timestamp(record_epoch_seconds(record)),
        "type": "hook_failed",
        "event_id": "evt-" + digest,
        "source": _HOOK_FAILURE_SOURCE,
        "session_id": session_id,
        "parent_session_id": parent_session_id,
        "hook_event": update.get("event_name"),
        "hook_name": run_name,
        "error": error if isinstance(error, str) else None,
        "prompt_id": update.get("prompt_id"),
    }
    return event


def _build_agent_identity_fields(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        field_name: environ[env_name]
        for env_name, field_name in (("MNGR_AGENT_ID", "agent_id"), ("MNGR_AGENT_NAME", "agent_name"))
        if environ.get(env_name)
    }


def _collect_hook_failures(
    record: Mapping[str, Any],
    session_id: str,
    parent_session_id: Optional[str],
    offset: int,
    follow_ups: dict[str, Any],
) -> None:
    runs = _record_update(record).get("runs")
    if not isinstance(runs, list):
        return
    for run_index, run in enumerate(runs):
        run_fields = _as_dict(run)
        name = run_fields.get("name")
        status = run_fields.get("status")
        if not isinstance(name, str) or not name.startswith(_MNGR_HOOK_RUN_PREFIX) or not isinstance(status, dict):
            continue
        if status.get("status") == "failed" and status.get("blocked") is not True:
            follow_ups["hook_failure_events"].append(
                _build_hook_failure_event(
                    record, session_id, parent_session_id, offset, run_index, name, status.get("error")
                )
            )


def scan_native_record(
    record: Mapping[str, Any],
    file_key: str,
    session_id: str,
    parent_session_id: Optional[str],
    offset: int,
    state: dict[str, Any],
    follow_ups: dict[str, Any],
) -> Optional[str]:
    """Collect the follow-up work one native record calls for; return the key of a newly discovered child file."""
    if is_copied_record(record, session_id):
        return None
    update = _record_update(record)
    kind = update.get("sessionUpdate")
    if kind == "turn_completed":
        prompt_id = update.get("prompt_id")
        if parent_session_id is None and isinstance(prompt_id, str) and prompt_id:
            follow_ups["turn_completions"].append([session_id, prompt_id])
    elif kind == "hook_execution":
        _collect_hook_failures(record, session_id, parent_session_id, offset, follow_ups)
    elif kind == "subagent_spawned":
        child_session_id = update.get("child_session_id") or update.get("subagent_id")
        if _is_valid_session_id(child_session_id):
            bucket = file_key.split("/", 1)[0]
            child_key = "{}/{}/updates.jsonl".format(bucket, child_session_id)
            if child_key not in state["files"]:
                state["files"][child_key] = {
                    "offset": 0,
                    "session_id": child_session_id,
                    "parent_session_id": session_id,
                }
                return child_key
    else:
        pass
    return None


def _read_new_complete_lines(file_path: Path, offset: int) -> bytes:
    with file_path.open("rb") as native_file:
        native_file.seek(offset)
        chunk = native_file.read()
    last_newline = chunk.rfind(b"\n")
    return chunk[: last_newline + 1] if last_newline >= 0 else b""


def _stream_session_file(
    sessions_dir: Path, file_key: str, state: dict[str, Any], follow_ups: dict[str, Any]
) -> tuple[bytes, list[str]]:
    """Tag the new complete lines of one session file; return them and any newly discovered child file keys."""
    entry = state["files"][file_key]
    file_path = sessions_dir / file_key
    try:
        file_size = file_path.stat().st_size
    except FileNotFoundError:
        return b"", []
    offset = entry["offset"]
    if file_size < offset:
        _LOGGER.warning("Restarted %s from offset 0: it shrank below offset %d", file_key, offset)
        offset = 0
    entry["scanned_size"] = file_size
    if file_size == offset:
        entry["offset"] = offset
        return b"", []
    complete = _read_new_complete_lines(file_path, offset)
    tagged_lines: list[bytes] = []
    child_keys: list[str] = []
    line_offset = offset
    for line in complete.splitlines(keepends=True):
        line_bytes = line.rstrip(b"\n")
        if line_bytes.strip():
            tagged_line, record = _build_tagged_line(
                entry["session_id"], entry["parent_session_id"], line_offset, line_bytes
            )
            tagged_lines.append(tagged_line)
            if record is not None:
                child_key = scan_native_record(
                    record,
                    file_key,
                    entry["session_id"],
                    entry["parent_session_id"],
                    line_offset,
                    state,
                    follow_ups,
                )
                if child_key is not None:
                    child_keys.append(child_key)
        line_offset += len(line)
    entry["offset"] = line_offset
    return b"".join(tagged_lines), child_keys


def _append_bytes_on_a_fresh_line(output_path: Path, data: bytes) -> None:
    """Append ``data``, first ending a torn last line so the appended lines never join it."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("ab+") as output_file:
        output_file.seek(0, os.SEEK_END)
        if output_file.tell() > 0:
            output_file.seek(-1, os.SEEK_END)
            if output_file.read(1) != b"\n":
                output_file.write(b"\n")
        output_file.write(data)


def _register_root_files(sessions_dir: Path, root_session_ids: Sequence[str], state: dict[str, Any]) -> list[str]:
    root_keys: list[str] = []
    for session_id in root_session_ids:
        for file_key in _find_session_file_keys(sessions_dir, session_id):
            if file_key not in state["files"]:
                state["files"][file_key] = {"offset": 0, "session_id": session_id, "parent_session_id": None}
            root_keys.append(file_key)
    return root_keys


def has_unread_bytes(sessions_dir: Path, root_session_ids: Sequence[str], state: Mapping[str, Any]) -> bool:
    candidate_keys = list(state["files"])
    for session_id in root_session_ids:
        candidate_keys.extend(_find_session_file_keys(sessions_dir, session_id))
    for file_key in candidate_keys:
        try:
            file_size = (sessions_dir / file_key).stat().st_size
        except FileNotFoundError:
            continue
        entry = state["files"].get(file_key)
        if entry is None or file_size != entry.get("scanned_size", entry.get("offset")):
            return True
    return False


def stream_new_lines_locked(
    state_dir: Path, root_session_ids: Sequence[str], environ: Mapping[str, str]
) -> dict[str, Any]:
    """Append every new complete line to the raw transcript; the caller holds the convert lock.

    Hook-failure events name the agent from ``MNGR_AGENT_ID`` and ``MNGR_AGENT_NAME`` in ``environ``.
    """
    sessions_dir = sessions_dir_path(state_dir)
    state = load_streamer_state(streamer_state_path(state_dir))
    follow_ups = new_follow_ups()
    pending_keys = _register_root_files(sessions_dir, root_session_ids, state)
    pending_keys.extend(key for key in state["files"] if key not in pending_keys)
    appended_chunks: list[bytes] = []
    processed_keys: set[str] = set()
    while pending_keys:
        file_key = pending_keys.pop(0)
        if file_key in processed_keys:
            continue
        processed_keys.add(file_key)
        tagged_bytes, child_keys = _stream_session_file(sessions_dir, file_key, state, follow_ups)
        appended_chunks.append(tagged_bytes)
        pending_keys.extend(child_keys)
    appended = b"".join(appended_chunks)
    if appended:
        _append_bytes_on_a_fresh_line(_raw_transcript_path(state_dir), appended)
    if follow_ups["hook_failure_events"]:
        agent_identity_fields = _build_agent_identity_fields(environ)
        events = "".join(
            json.dumps({**event, **agent_identity_fields}) + "\n" for event in follow_ups["hook_failure_events"]
        )
        _append_bytes_on_a_fresh_line(_hook_failure_events_path(state_dir), events.encode("utf-8"))
        for event in follow_ups["hook_failure_events"]:
            _LOGGER.warning(
                "Recorded failed hook run %s in session %s: %s",
                event["hook_name"],
                event["session_id"],
                event["error"],
            )
    streamer_state_path(state_dir).parent.mkdir(parents=True, exist_ok=True)
    _save_streamer_state(streamer_state_path(state_dir), state)
    return follow_ups


def run_follow_up_command(arguments: Sequence[str], environ: Mapping[str, str]) -> None:
    try:
        completed = subprocess.run(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=dict(environ),
            timeout=_FOLLOW_UP_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _LOGGER.warning("Timed out after %.0fs running %s", _FOLLOW_UP_TIMEOUT_SECONDS, " ".join(arguments))
        return
    except OSError as error:
        _LOGGER.warning("Failed to run %s: %s", " ".join(arguments), error)
        return
    if completed.returncode != 0:
        _LOGGER.warning(
            "Command %s exited %d: %s",
            " ".join(arguments),
            completed.returncode,
            completed.stderr.decode("utf-8", errors="replace").strip(),
        )


def _run_follow_ups(state_dir: Path, follow_ups: Mapping[str, Any], environ: Mapping[str, str]) -> None:
    """Flush the common transcript and notify the hook handler, outside the convert lock."""
    hook_path = str(_hook_handler_path(state_dir))
    if not follow_ups["turn_completions"]:
        return
    common_transcript_script = state_dir / "commands" / "common_transcript.sh"
    if common_transcript_script.is_file():
        run_follow_up_command(["bash", str(common_transcript_script), "--single-pass", "--hold-open-steps"], environ)
    for session_id, prompt_id in follow_ups["turn_completions"]:
        run_follow_up_command([hook_path, "--turn-completed", session_id, prompt_id], environ)


def run_stream_pass(state_dir: Path, environ: Mapping[str, str]) -> None:
    """Stream every new line once, then run the follow-ups the appended records call for."""
    root_session_ids = read_root_session_ids(_grok_plugin_dir(state_dir) / "roots")
    sessions_dir = sessions_dir_path(state_dir)
    if not has_unread_bytes(sessions_dir, root_session_ids, load_streamer_state(streamer_state_path(state_dir))):
        return
    lock_path = convert_lock_path(state_dir)
    with termination_signals_deferred():
        if not acquire_convert_lock(lock_path, read_lock_timeout_seconds(environ)):
            if not is_termination_pending():
                _LOGGER.warning("Deferred a pass: the convert lock at %s stayed held", lock_path)
            return
        try:
            follow_ups = stream_new_lines_locked(state_dir, root_session_ids, environ)
        finally:
            release_convert_lock(lock_path)
    _run_follow_ups(state_dir, follow_ups, environ)


def run_streamer(state_dir: Path, is_single_pass: bool, environ: Mapping[str, str]) -> None:
    """Run one pass, or poll every POLL_INTERVAL_SECONDS until the agent state directory is gone."""
    if is_single_pass:
        run_stream_pass(state_dir, environ)
        return
    _LOGGER.info("Started streaming raw grok transcripts (pid %d)", os.getpid())
    while state_dir.is_dir():
        run_stream_pass(state_dir, environ)
        time.sleep(POLL_INTERVAL_SECONDS)


def main(arguments: Sequence[str]) -> int:
    state_dir = Path(os.environ["MNGR_AGENT_STATE_DIR"])
    is_single_pass = "--single-pass" in arguments
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    configure_logging(state_dir)
    run_streamer(state_dir, is_single_pass, os.environ)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
