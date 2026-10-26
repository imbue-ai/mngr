import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final
from typing import TypedDict

from imbue.minds.workspace_programs.backup_protocol import BackupProgramStatus
from imbue.minds.workspace_programs.backup_protocol import BackupServiceState
from imbue.minds.workspace_programs.backup_protocol import OFFICIAL_REMOTE_NAME
from imbue.minds.workspace_programs.program_support import as_json_object
from imbue.minds.workspace_programs.program_support import as_json_object_list
from imbue.minds.workspace_programs.program_support import poll_until
from imbue.minds.workspace_programs.program_support import run_command

# The backup-service code inside the workspace: system/services/host_backup on
# the creation-rename template layout, system/libs/host_backup on the earlier
# decluttered layout, libs/host_backup on pre-declutter workspaces.
_BACKUP_CODE_PATH_CANDIDATES: Final[tuple[str, ...]] = (
    "system/services/host_backup",
    "system/libs/host_backup",
    "libs/host_backup",
)
RESTIC_ENV_PATH: Final[str] = "data/.secrets/restic.env"
# The template's background-task CLI; a workspace whose template predates it has no busy chats to report.
BACKGROUND_TASKS_CLI_PATH: Final[str] = "system/scripts/background_tasks.py"
GIT_IDENTITY: Final[tuple[str, ...]] = (
    "-c",
    "user.name=minds-backup-update",
    "-c",
    "user.email=backup-update@minds.local",
)
_TICK_COMPLETION_TYPES: Final[tuple[str, ...]] = (
    "RESTIC_BACKUP_SUCCEEDED",
    "RESTIC_BACKUP_FAILED",
    "TICK_SKIPPED_DUE_TO_MISSING_SECRETS",
    "TICK_ERROR",
    "SNAPSHOT_FAILED",
)
# Imbue Studio already waited (unboundedly, cancellably) for a quiet workspace before
# dispatching a mutating program; this bounded wait only covers a tick that
# started in between, and must stay well inside the caller's outer exec
# timeout so a structured "timed out waiting" payload beats the exec being
# killed.
_TICK_WAIT_TIMEOUT_SECONDS: Final[float] = 900.0
# How much of the end of the backup events log the in-flight check reads. Events
# embed restic's full output, so the log reaches gigabytes on an old workspace;
# the events a tick writes before it completes are small, so an in-flight tick
# always falls inside this window.
_EVENTS_TAIL_MAX_BYTES: Final[int] = 8 * 1024 * 1024
_TICK_POLL_SECONDS: Final[float] = 5.0
SERVICE_VERIFY_TIMEOUT_SECONDS: Final[float] = 60.0
SHORT_COMMAND_TIMEOUT_SECONDS: Final[float] = 60.0
MNGR_COMMAND_TIMEOUT_SECONDS: Final[float] = 180.0
_TAG_FETCH_TIMEOUT_SECONDS: Final[float] = 300.0


class RunningChatsGate(TypedDict):
    """The chats that hold a mutating program back, and the background runners that would wake each one again."""

    chats: list[str]
    runner_pids_by_chat: dict[str, list[int]]


def resolve_workspace_backup_code_path() -> str:
    """The backup-service directory in this workspace's own tree (cwd is always the workspace root).

    Pathspecs addressed at a *tag's* tree follow that tag's own layout instead
    (``backup_code_path_in_tree``), since the layouts differ across the renames.
    Resolve this once per run: a program that rewrites the directory changes the answer.
    """
    return next(
        (path for path in _BACKUP_CODE_PATH_CANDIDATES[:-1] if os.path.isdir(path)),
        _BACKUP_CODE_PATH_CANDIDATES[-1],
    )


def run_mngr(args: Sequence[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    """Run `uv run mngr <args>` in this workspace, tolerating config it cannot parse.

    The workspace's .mngr/settings.toml is versioned with its template while the
    mngr that parses it is installed separately, so it can name config a plugin
    this mngr does not have yet. Strict parsing (mngr's default) would then fail
    every command here before it did anything -- including the chat gate that
    decides whether the very update carrying the missing plugin may run.
    """
    env = dict(os.environ)
    env["MNGR_ALLOW_UNKNOWN_CONFIG"] = "1"
    return run_command(["uv", "run", "mngr", *args], timeout_seconds, env=env)


def output_tail(completed: subprocess.CompletedProcess[str], max_chars: int) -> str:
    """The end of a command's stderr (or its stdout when stderr is empty), for a failure detail."""
    return (completed.stderr or completed.stdout).strip()[-max_chars:]


def tag_exists(tag: str) -> bool:
    return (
        run_command(
            ["git", "rev-parse", "-q", "--verify", "refs/tags/%s" % tag], SHORT_COMMAND_TIMEOUT_SECONDS
        ).returncode
        == 0
    )


def backup_code_path_in_tree(ref: str) -> str:
    """The backup-service path as laid out in ref's tree, or "" if absent.

    Tags cut before the workspace-root declutter store the code at
    libs/host_backup; decluttered trees at system/libs/host_backup; the
    creation-rename layout at system/services/host_backup. The workspace's own
    layout and a target tag's layout can therefore differ in either direction,
    and diff/checkout pathspecs must follow the tree they address.
    """
    for candidate in _BACKUP_CODE_PATH_CANDIDATES:
        verified = run_command(
            ["git", "rev-parse", "-q", "--verify", "%s:%s" % (ref, candidate)], SHORT_COMMAND_TIMEOUT_SECONDS
        )
        if verified.returncode == 0:
            return candidate
    return ""


def _ensure_official_remote(official_url: str) -> str:
    """Idempotently point the `official` remote at the official template URL; "" or an error detail.

    Imbue Studio owns this remote name: a missing remote is added and a remote
    pointing anywhere else is repointed, so the fetch below always talks to
    the official repository regardless of what the workspace was created from.
    """
    current = run_command(["git", "remote", "get-url", OFFICIAL_REMOTE_NAME], SHORT_COMMAND_TIMEOUT_SECONDS)
    if current.returncode == 0:
        if current.stdout.strip() == official_url:
            return ""
        repointed = run_command(
            ["git", "remote", "set-url", OFFICIAL_REMOTE_NAME, official_url], SHORT_COMMAND_TIMEOUT_SECONDS
        )
        if repointed.returncode != 0:
            return "cannot repoint official remote: %s" % repointed.stderr.strip()
        return ""
    added = run_command(["git", "remote", "add", OFFICIAL_REMOTE_NAME, official_url], SHORT_COMMAND_TIMEOUT_SECONDS)
    if added.returncode != 0:
        return "cannot add official remote: %s" % added.stderr.strip()
    return ""


def fetch_official_tags(official_url: str) -> str:
    """Fetch every tag from the official remote, pointed at ``official_url`` first; "" or an error detail."""
    remote_error = _ensure_official_remote(official_url)
    if remote_error:
        return remote_error
    fetched = run_command(["git", "fetch", OFFICIAL_REMOTE_NAME, "--tags", "--quiet"], _TAG_FETCH_TIMEOUT_SECONDS)
    if fetched.returncode != 0:
        return "git fetch official --tags failed: %s" % output_tail(fetched, 500)
    return ""


def tag_sort_key(tag: str) -> list[int]:
    """A sort key for a ``minds-v*`` tag: its version's leading digits, part by part."""
    key: list[int] = []
    for part in tag[len("minds-v") :].split("."):
        digits = ""
        for char in part:
            if char.isdigit():
                digits += char
            else:
                break
        key.append(int(digits) if digits else 0)
    return key


def service_state() -> tuple[BackupServiceState, str]:
    """``(state, detail)`` of the host-backup program: running | not_running | unknown."""
    status = run_command(["supervisorctl", "status", "host-backup"], SHORT_COMMAND_TIMEOUT_SECONDS)
    text = (status.stdout or "").strip()
    if text and "RUNNING" in text.split():
        return BackupServiceState.RUNNING, text
    if text:
        return BackupServiceState.NOT_RUNNING, text
    return BackupServiceState.UNKNOWN, (status.stderr or "supervisorctl produced no output").strip()


def _read_background_chats() -> list[dict[str, object]]:
    """The chats with pending background tasks, as the template's CLI lists them.

    Each is the CLI's JSON record: the chat's active agent (``active_agent_name`` empty and
    ``active_agent_id`` the chat's own id when the CLI read the marker files rather than
    the chat app), whether the chat is busy (its agent will resume on its own), and its
    tasks. A workspace without the CLI has none, and so does a CLI that fails, noted on stderr.
    """
    if not os.path.isfile(BACKGROUND_TASKS_CLI_PATH):
        return []
    listed = run_command(
        [sys.executable, BACKGROUND_TASKS_CLI_PATH, "list", "--format", "json"], SHORT_COMMAND_TIMEOUT_SECONDS
    )
    if listed.returncode != 0:
        sys.stderr.write(
            "%s list failed (exit %d), so no chat reads as busy: %s\n"
            % (BACKGROUND_TASKS_CLI_PATH, listed.returncode, output_tail(listed, 300))
        )
        return []
    try:
        payload: object = json.loads(listed.stdout)
    except ValueError:
        sys.stderr.write(
            "%s list printed no JSON, so no chat reads as busy: %s\n"
            % (BACKGROUND_TASKS_CLI_PATH, listed.stdout.strip()[-300:])
        )
        return []
    listing = as_json_object(payload)
    return as_json_object_list(listing.get("chats")) if listing is not None else []


def _background_chats_of(
    agent: Mapping[str, object], background_chats: Sequence[Mapping[str, object]]
) -> list[Mapping[str, object]]:
    """The background chats whose active agent is ``agent``, matched by name or by id.

    A chat the CLI read from the marker files names no agent and gives its own id as its
    agent's, which is the id of the chat's first agent; an agent the chat was handed off to
    is matched to it by its ``chat_id`` label instead.
    """
    name = str(agent.get("name") or "")
    agent_id = str(agent.get("id") or "")
    labels = as_json_object(agent.get("labels"))
    chat_label = str(labels.get("chat_id") or "") if labels is not None else ""
    return [
        chat
        for chat in background_chats
        if (name and chat.get("active_agent_name") == name)
        or (agent_id and chat.get("active_agent_id") == agent_id)
        or (chat_label and not chat.get("active_agent_name") and chat.get("chat_id") == chat_label)
    ]


def _runner_pids(background_chats: Sequence[Mapping[str, object]]) -> list[int]:
    """The ``run_in_background.py`` runners of these chats; Claude's own tasks die with Claude."""
    pids: list[int] = []
    for chat in background_chats:
        for task in as_json_object_list(chat.get("background_tasks")):
            if task.get("source") != "run_in_background":
                continue
            pid = task.get("pid")
            if isinstance(pid, int):
                pids.append(pid)
    return pids


def _agents_payload_from_mngr_list(stdout: str) -> dict[str, object] | None:
    """The last JSON line of `mngr list` output that carries an ``agents`` field."""
    for line in reversed(stdout.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            candidate: object = json.loads(stripped)
        except ValueError:
            continue
        listing = as_json_object(candidate)
        if listing is not None and "agents" in listing:
            return listing
    return None


def list_running_chats() -> tuple[RunningChatsGate | None, str]:
    """``(gate, error)``; the gate is None when the chats could not be listed.

    The gate's ``chats`` are the agents in this work_dir, excluding type=main, that are RUNNING,
    UNKNOWN (their state could not be read, so they may be mid-turn), or WAITING with their chat
    busy (they will resume on their own). ``runner_pids_by_chat`` holds each chat's runners, the
    background commands that would wake it again after a stop.
    """
    listed = run_mngr(["list", "--format", "json", "--provider", "local"], MNGR_COMMAND_TIMEOUT_SECONDS)
    if listed.returncode != 0:
        return None, "mngr list failed: %s" % output_tail(listed, 500)
    payload = _agents_payload_from_mngr_list(listed.stdout)
    if payload is None:
        return None, "mngr list produced no JSON payload"
    background_chats = _read_background_chats()
    cwd = os.path.realpath(os.getcwd())
    chats: list[str] = []
    runner_pids_by_chat: dict[str, list[int]] = {}
    for agent in as_json_object_list(payload.get("agents")):
        work_dir = os.path.realpath(str(agent.get("work_dir", "")))
        if work_dir != cwd:
            continue
        if agent.get("type") == "main":
            continue
        own_background_chats = _background_chats_of(agent, background_chats)
        is_busy = any(chat.get("is_busy") is True for chat in own_background_chats)
        state = agent.get("state")
        if state not in ("RUNNING", "UNKNOWN") and not (state == "WAITING" and is_busy):
            continue
        chat_name = str(agent.get("name") or agent.get("id") or "unknown")
        chats.append(chat_name)
        runner_pids_by_chat[chat_name] = _runner_pids(own_background_chats)
    return {"chats": chats, "runner_pids_by_chat": runner_pids_by_chat}, ""


def _end_runners(runner_pids: Sequence[int]) -> None:
    """End each runner and its command; a runner leads its own session, so its group is both."""
    for pid in runner_pids:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _backup_events_path(agent_id: str) -> str:
    state_dir = os.environ.get("MNGR_AGENT_STATE_DIR", "")
    if not state_dir and agent_id:
        host_dir = os.environ.get("MNGR_HOST_DIR", "/home/user/.mngr")
        state_dir = os.path.join(host_dir, "agents", agent_id)
    if not state_dir:
        return ""
    return os.path.join(state_dir, "events", "backup", "events.jsonl")


def _read_events_tail_lines(events_path: str) -> list[str] | None:
    """The complete lines in the last window of the events log, or None when it cannot be read."""
    try:
        with open(events_path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - _EVENTS_TAIL_MAX_BYTES))
            lines = fh.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    # A window that starts mid-file almost certainly cut its first line in half.
    if size > _EVENTS_TAIL_MAX_BYTES and lines:
        return lines[1:]
    return lines


def is_backup_tick_in_flight(agent_id: str) -> bool:
    """Whether a backup tick is running right now, per the event journal."""
    # Ticks are restic runs owned by the supervised host-backup service; when
    # that service is not RUNNING (or supervisord is unreachable) no tick can
    # be alive, and any started-but-unfinished journal entry is an orphan from
    # a killed tick (e.g. a restore's `stop all`), not live work to wait for.
    state, _ = service_state()
    if state != BackupServiceState.RUNNING:
        return False
    events_path = _backup_events_path(agent_id)
    if not events_path or not os.path.isfile(events_path):
        return False
    lines = _read_events_tail_lines(events_path)
    if lines is None:
        return False
    # Ticks run serially in one loop, so only the most recently started tick
    # can be in flight. A tick killed mid-flight (e.g. by a service restart, or
    # by the stop a restore does) never writes its completion event; treating
    # every started-but-unfinished tick as in flight would let one such dead
    # tick block operations for hours, until its line scrolls out of the
    # window. Scoping to the last started tick means the next tick to start
    # supersedes any orphan a restore leaves behind.
    last_started_tick: str | None = None
    finished: set[str] = set()
    for raw in lines[-200:]:
        try:
            event = as_json_object(json.loads(raw))
        except ValueError:
            continue
        if event is None:
            continue
        tick_id = event.get("tick_id")
        if not isinstance(tick_id, str):
            continue
        event_type = event.get("type")
        if event_type == "BACKUP_STARTED":
            last_started_tick = tick_id
        elif event_type in _TICK_COMPLETION_TYPES:
            finished.add(tick_id)
        else:
            pass
    return last_started_tick is not None and last_started_tick not in finished


def _stop_running_chats(gate: RunningChatsGate) -> str:
    """Stop every chat the gate names, ending its runners first; "" or an error detail."""
    for chat_name in gate["chats"]:
        # First, so no runner reports its killed command and wakes the chat again.
        _end_runners(gate["runner_pids_by_chat"].get(chat_name, []))
        stopped = run_mngr(["stop", chat_name], MNGR_COMMAND_TIMEOUT_SECONDS)
        if stopped.returncode != 0:
            return "could not stop chat %s: %s" % (chat_name, output_tail(stopped, 300))
    return ""


def gate_chats_and_wait_for_tick(
    agent_id: str, is_stop_chats: bool, is_chat_gate_skipped: bool
) -> tuple[BackupProgramStatus, dict[str, object], str]:
    """Gate a mutating program: no running or busy chats (optionally stop them), no in-flight tick.

    Returns (status, extra, detail) with status in ok | blocked | failed;
    ``extra`` carries payload fields (e.g. running_chats) for the result.
    ``is_chat_gate_skipped`` bypasses only the chat half (the user explicitly
    forced a restore on a workspace that can no longer answer `mngr list`);
    the tick wait still applies -- it needs no workspace code to answer.
    """
    if not is_chat_gate_skipped:
        gate, gate_error = list_running_chats()
        if gate is None:
            return BackupProgramStatus.FAILED, {}, "cannot determine running chats: %s" % gate_error
        if gate["chats"] and is_stop_chats:
            stop_error = _stop_running_chats(gate)
            if stop_error:
                return BackupProgramStatus.FAILED, {}, stop_error
            gate, gate_error = list_running_chats()
            if gate is None:
                return BackupProgramStatus.FAILED, {}, "cannot re-check running chats: %s" % gate_error
        if gate["chats"]:
            return BackupProgramStatus.BLOCKED, {"running_chats": gate["chats"]}, "chat agents are running"
    is_tick_still_in_flight = poll_until(
        lambda: is_backup_tick_in_flight(agent_id),
        lambda is_in_flight: not is_in_flight,
        _TICK_WAIT_TIMEOUT_SECONDS,
        _TICK_POLL_SECONDS,
    )
    if is_tick_still_in_flight:
        return BackupProgramStatus.FAILED, {}, "timed out waiting for the in-flight backup tick to finish"
    return BackupProgramStatus.OK, {}, ""
