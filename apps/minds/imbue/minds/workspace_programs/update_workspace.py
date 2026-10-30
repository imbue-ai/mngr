"""The Imbue Studio app's in-workspace update program: install, check, arm, start, cancel, read, and tick.

Every update action the app takes on a workspace is one ``mngr exec`` of this program, run from the
workspace root. Each mode is idempotent and prints one marker-wrapped JSON answer:

- ``arm``: install the dispatcher and the cron line, check the update-self skill, cron, and a
  signed-in account, then write the schedule file with the ceiling, the window (in UTC, from the
  user's local hours), and any version the user typed;
- ``now``: the same installs and checks (cron is not needed), then create the update chat with
  the auto-open label and return once its create has finished;
- ``cancel``: delete the schedule file;
- ``read``: the schedule file, the run record and the last dispatch, the detection sweep's one
  read, with the id of the chat the run record names when the last dispatch does not name it;
  given the app's current values, an armed file is first refreshed with them, and asked, it says
  whether an account is signed in;
- ``tick``: the dispatcher's own minute (``update_dispatcher``), run by cron from the installed copy;
- ``busy``: the chats the workspace's background-task marker files hold live tasks for, which the app's run
  probe reads from the installed copy in a template without the background-task CLI.

The program ships as a zip application, and ``arm``, ``now`` and a refreshing ``read`` copy that
archive into the workspace as ``data/.state/update-apply/dispatcher.pyz``, so the dispatcher cron runs
is always the code of the last app that touched it.
"""

import os
import sys
import time
from collections.abc import Callable
from typing import Final
from typing import TypedDict

from imbue.minds.workspace_programs.background_task_markers import busy_chat_ids
from imbue.minds.workspace_programs.update_dispatcher import AGENT_PIDS_DIR
from imbue.minds.workspace_programs.update_dispatcher import AgentEnvironment
from imbue.minds.workspace_programs.update_dispatcher import CreateResult
from imbue.minds.workspace_programs.update_dispatcher import DISPATCHER_ARCHIVE_PATH
from imbue.minds.workspace_programs.update_dispatcher import DISPATCHER_LOG_PATH
from imbue.minds.workspace_programs.update_dispatcher import PROC_ROOT
from imbue.minds.workspace_programs.update_dispatcher import ProgramContext
from imbue.minds.workspace_programs.update_dispatcher import UPDATE_PROGRAM_SCHEMA
from imbue.minds.workspace_programs.update_dispatcher import chat_id_of
from imbue.minds.workspace_programs.update_dispatcher import create_result
from imbue.minds.workspace_programs.update_dispatcher import create_update_chat
from imbue.minds.workspace_programs.update_dispatcher import emit_update_answer
from imbue.minds.workspace_programs.update_dispatcher import find_run_lead
from imbue.minds.workspace_programs.update_dispatcher import is_cron_available
from imbue.minds.workspace_programs.update_dispatcher import is_run_in_flight
from imbue.minds.workspace_programs.update_dispatcher import is_update_lease_held
from imbue.minds.workspace_programs.update_dispatcher import is_update_skill_present
from imbue.minds.workspace_programs.update_dispatcher import list_agents
from imbue.minds.workspace_programs.update_dispatcher import mode_tick
from imbue.minds.workspace_programs.update_dispatcher import pending_dispatched_chat
from imbue.minds.workspace_programs.update_dispatcher import read_json_object
from imbue.minds.workspace_programs.update_dispatcher import resolve_account
from imbue.minds.workspace_programs.update_dispatcher import run_started_at
from imbue.minds.workspace_programs.update_dispatcher import write_file_if_changed
from imbue.minds.workspace_programs.update_dispatcher import write_json_atomically
from imbue.minds.workspace_programs.update_dispatcher import write_log_line
from imbue.minds.workspace_programs.update_protocol import BUSY_CHAT_IDS_FIELD
from imbue.minds.workspace_programs.update_protocol import BUSY_MODE
from imbue.minds.workspace_programs.update_protocol import DISPATCHER_CRON_NAME
from imbue.minds.workspace_programs.update_protocol import DURABLE_CRON_PATH
from imbue.minds.workspace_programs.update_protocol import LAST_DISPATCH_FILE_PATH
from imbue.minds.workspace_programs.update_protocol import LIVE_CRON_DIRECTORY
from imbue.minds.workspace_programs.update_protocol import OUTCOME_ALREADY_RUNNING
from imbue.minds.workspace_programs.update_protocol import OUTCOME_ARMED
from imbue.minds.workspace_programs.update_protocol import OUTCOME_NEEDS_SIGN_IN
from imbue.minds.workspace_programs.update_protocol import OUTCOME_NO_CRON
from imbue.minds.workspace_programs.update_protocol import OUTCOME_TOO_OLD_TO_UPDATE
from imbue.minds.workspace_programs.update_protocol import RUN_RECORD_PATH
from imbue.minds.workspace_programs.update_protocol import SCHEDULE_FILE_PATH
from imbue.minds.workspace_programs.update_protocol import SKIP_NEEDS_SIGN_IN
from imbue.minds.workspace_programs.update_protocol import STARTED_BY_IMMEDIATE
from imbue.minds.workspace_programs.update_protocol import UPDATE_COMMAND_CHECK_SIGN_IN_FLAG

_CRON_LOCK_PATH: Final[str] = "/run/update-self-dispatch.lock"
_CRON_PATH_LINE: Final[str] = "PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_CRON_FILE_MODE: Final[int] = 0o644
_USAGE_EXIT_CODE: Final[int] = 2


def _cron_line(workspace_root: str) -> str:
    """The drop-in cron runs the dispatcher from: its own PATH and working directory, one tick at a time."""
    tick = "cd %s && python3 %s tick >> %s 2>&1" % (workspace_root, DISPATCHER_ARCHIVE_PATH, DISPATCHER_LOG_PATH)
    return "%s\n* * * * * root flock -n %s -c '%s'\n" % (_CRON_PATH_LINE, _CRON_LOCK_PATH, tick)


def _install_dispatcher(context: ProgramContext) -> None:
    """Copy this program's archive into the workspace as its dispatcher, so it is the app's current code."""
    with open(context["program_path"], "rb") as handle:
        archive = handle.read()
    if write_file_if_changed(DISPATCHER_ARCHIVE_PATH, archive, None):
        write_log_line("installed the dispatcher")


def _install_cron_entry(context: ProgramContext) -> bool:
    """Write the cron line's durable copy (reinstalled at boot) and its live copy; whether cron will run it."""
    if not is_cron_available(context["live_cron_dir"]):
        return False
    line = _cron_line(os.path.realpath(os.getcwd())).encode("utf-8")
    write_file_if_changed(DURABLE_CRON_PATH, line, _CRON_FILE_MODE)
    try:
        write_file_if_changed(os.path.join(context["live_cron_dir"], DISPATCHER_CRON_NAME), line, _CRON_FILE_MODE)
    except OSError as exc:
        write_log_line("could not install the live cron entry: %s" % exc)
        return False
    return True


def _arg(context: ProgramContext, flag: str, default: str = "") -> str:
    argv = context["argv"]
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return argv[index + 1]
    return default


class _AppFields(TypedDict):
    """The schedule fields the app owns."""

    ceiling: str
    window_start_minute_utc: int
    window_length_minutes: int
    is_backup_configured: bool


def _app_fields(context: ProgramContext) -> _AppFields:
    """The schedule fields the app owns, from this run's arguments."""
    return {
        "ceiling": _arg(context, "--ceiling"),
        "window_start_minute_utc": int(_arg(context, "--window-start-minute-utc")),
        "window_length_minutes": int(_arg(context, "--window-length-minutes")),
        "is_backup_configured": _arg(context, "--backup-configured", "1") == "1",
    }


def _readiness_refusal(
    context: ProgramContext, is_cron_needed: bool
) -> tuple[CreateResult | None, tuple[bool, list[str]] | None]:
    """``(refusal, account)``: the outcome that stops an arm or a now before it does anything (None to go
    ahead), and what ``resolve_account`` answered once the checks got that far (None before)."""
    if is_cron_needed and not is_cron_available(context["live_cron_dir"]):
        detail = "This workspace has no cron program, so nothing in it could start a scheduled update."
        return create_result(OUTCOME_NO_CRON, detail), None
    if not is_update_skill_present():
        return create_result(OUTCOME_TOO_OLD_TO_UPDATE, "This workspace predates the update-self skill."), None
    account = resolve_account(context["agent_environment"])
    if not account[0]:
        return create_result(OUTCOME_NEEDS_SIGN_IN, "No AI account is signed in to this workspace."), account
    return None, account


def mode_arm(context: ProgramContext) -> None:
    _install_dispatcher(context)
    refusal, _ = _readiness_refusal(context, is_cron_needed=True)
    if refusal is not None:
        emit_update_answer({**refusal, "mode": "arm", "schedule": read_json_object(SCHEDULE_FILE_PATH)})
        return
    if not _install_cron_entry(context):
        detail = "This workspace's cron could not be given the update's schedule line."
        emit_update_answer(
            {**create_result(OUTCOME_NO_CRON, detail), "mode": "arm", "schedule": read_json_object(SCHEDULE_FILE_PATH)}
        )
        return
    existing = read_json_object(SCHEDULE_FILE_PATH) or {}
    app_fields = _app_fields(context)
    override_ref = _arg(context, "--override")
    schedule: dict[str, object] = {
        "schema": UPDATE_PROGRAM_SCHEMA,
        "armed_at": context["now"],
        "marked_chat": existing.get("marked_chat", ""),
        "marked_at": existing.get("marked_at"),
        "window_seen_open_at": existing.get("window_seen_open_at"),
        "pending_skip_reason": "",
        "next_attempt_at": None,
        "last_skip_reason": "",
        "last_skip_at": None,
        **app_fields,
        "override_ref": override_ref,
        "is_auto_armed": _arg(context, "--auto-armed", "0") == "1",
    }
    write_json_atomically(SCHEDULE_FILE_PATH, schedule)
    start_minute_utc = app_fields["window_start_minute_utc"]
    write_log_line(
        "armed a schedule (%d minutes from %02d:%02d UTC, target %s)"
        % (
            app_fields["window_length_minutes"],
            start_minute_utc // 60,
            start_minute_utc % 60,
            override_ref or app_fields["ceiling"],
        )
    )
    emit_update_answer({**create_result(OUTCOME_ARMED), "mode": "arm", "schedule": schedule})


def mode_now(context: ProgramContext) -> None:
    agent_environment = context["agent_environment"]
    _install_dispatcher(context)
    # Only where cron runs the watchdog is a stopped lead brought back.
    is_watchdog_installed = _install_cron_entry(context)
    refusal, account = _readiness_refusal(context, is_cron_needed=False)
    if refusal is not None:
        emit_update_answer({**refusal, "mode": "now"})
        return
    assert account is not None
    agents = list_agents(agent_environment)
    run = read_json_object(RUN_RECORD_PATH)
    # An ALREADY_RUNNING answer names when the run it found began, which tells its record apart from an
    # earlier run's under the same chat name.
    if run is not None and is_run_in_flight(run, agents, is_stopped_lead_revived=is_watchdog_installed):
        lead = find_run_lead(agents or [], run)
        already_running = create_result(
            OUTCOME_ALREADY_RUNNING,
            "An update is already running in this workspace.",
            str(run["chat_agent_name"]),
            chat_id_of(lead) if lead else "",
        )
        emit_update_answer({**already_running, "mode": "now", "run_started_at": run_started_at(run)})
        return
    if is_update_lease_held(agents, agent_environment):
        flow_running = create_result(OUTCOME_ALREADY_RUNNING, "Another update flow is working in this workspace.")
        emit_update_answer({**flow_running, "mode": "now"})
        return
    pending = pending_dispatched_chat(run, agents, context["now"])
    if pending is not None:
        name, chat_id, dispatched_at = pending
        starting = create_result(
            OUTCOME_ALREADY_RUNNING, "An update is already starting in this workspace.", name, chat_id
        )
        emit_update_answer({**starting, "mode": "now", "run_started_at": dispatched_at})
        return
    fields: dict[str, object] = {
        "ceiling": _arg(context, "--ceiling"),
        "override_ref": _arg(context, "--override"),
        "is_backup_configured": _arg(context, "--backup-configured", "1") == "1",
    }
    created = create_update_chat(
        fields,
        STARTED_BY_IMMEDIATE,
        is_auto_open=True,
        agents=agents,
        now=context["now"],
        account=account,
        agent_environment=agent_environment,
    )
    emit_update_answer({**created, "mode": "now"})


def mode_cancel(context: ProgramContext) -> None:
    try:
        os.remove(SCHEDULE_FILE_PATH)
    except FileNotFoundError:
        emit_update_answer({"mode": "cancel", "is_cancelled": False})
        return
    write_log_line("cancelled the schedule")
    emit_update_answer({"mode": "cancel", "is_cancelled": True})


# Bounds ``read``'s lookup of a run's chat inside the app's timeout for the whole ``read``, so a slow listing
# costs the chat and not the answer.
_RUN_CHAT_LOOKUP_TIMEOUT_SECONDS: Final[float] = 45.0


def _run_chat_id(
    run: dict[str, object] | None, last_dispatch: dict[str, object] | None, agent_environment: AgentEnvironment
) -> str | None:
    """The chat the run record names, looked up when the last dispatch does not name it.

    That is a run started by hand in a chat, or one whose dispatch a later one replaced. None when there is
    nothing to look up or the workspace's agents could not be listed; "" when no such chat is listed.
    """
    if run is None:
        return None
    chat_name = run.get("chat_agent_name")
    if not chat_name or (last_dispatch or {}).get("chat_agent_name") == chat_name:
        return None
    agents = list_agents(agent_environment, timeout_seconds=_RUN_CHAT_LOOKUP_TIMEOUT_SECONDS)
    if agents is None:
        return None
    lead = find_run_lead(agents, run)
    return chat_id_of(lead) if lead is not None else ""


def mode_read(context: ProgramContext) -> None:
    """The schedule, the run record and the last dispatch in one answer.

    With app fields given, an armed schedule is first rewritten with them and the dispatcher brought
    current. Whether an account is signed in rides along when the app asks, or the schedule's skip says
    none was; the id of the chat the run record names, when the last dispatch does not name it.
    """
    schedule = read_json_object(SCHEDULE_FILE_PATH)
    if schedule is not None and _arg(context, "--ceiling"):
        fields = _app_fields(context)
        if any(schedule.get(key) != value for key, value in fields.items()):
            schedule.update(fields)
            write_json_atomically(SCHEDULE_FILE_PATH, schedule)
        _install_dispatcher(context)
        _install_cron_entry(context)
    is_skipped_for_sign_in = schedule is not None and SKIP_NEEDS_SIGN_IN in (
        schedule.get("pending_skip_reason"),
        schedule.get("last_skip_reason"),
    )
    is_sign_in_asked = UPDATE_COMMAND_CHECK_SIGN_IN_FLAG in context["argv"] or is_skipped_for_sign_in
    is_signed_in = resolve_account(context["agent_environment"])[0] if is_sign_in_asked else None
    run = read_json_object(RUN_RECORD_PATH)
    last_dispatch = read_json_object(LAST_DISPATCH_FILE_PATH)
    emit_update_answer(
        {
            "mode": "read",
            "schedule": schedule,
            "run": run,
            "last_dispatch": last_dispatch,
            "run_chat_id": _run_chat_id(run, last_dispatch, context["agent_environment"]),
            "is_signed_in": is_signed_in,
        }
    )


# CLEANUP: drop this mode once ``OLDEST_IN_PLACE_UPDATABLE_VERSION`` reaches minds-v0.8.7, the first release
# that ships ``system/scripts/background_tasks.py``.
def mode_busy(context: ProgramContext) -> None:
    """The chats the workspace's background-task marker files hold live tasks for."""
    emit_update_answer({"mode": BUSY_MODE, BUSY_CHAT_IDS_FIELD: sorted(busy_chat_ids())})


MODES: Final[dict[str, Callable[[ProgramContext], None]]] = {
    "arm": mode_arm,
    "now": mode_now,
    "cancel": mode_cancel,
    "read": mode_read,
    "tick": mode_tick,
    BUSY_MODE: mode_busy,
}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    run_mode = MODES.get(mode)
    if run_mode is None:
        sys.stderr.write("usage: %s {%s} [options]\n" % (sys.argv[0], "|".join(sorted(MODES))))
        return _USAGE_EXIT_CODE
    run_mode(
        {
            "argv": sys.argv[2:],
            # Run as ``python3 <archive>``, the program's own path is its archive.
            "program_path": os.path.abspath(sys.argv[0]),
            "live_cron_dir": LIVE_CRON_DIRECTORY,
            "proc_root": PROC_ROOT,
            "agent_pids_dir": AGENT_PIDS_DIR,
            "now": time.time(),
            "agent_environment": AgentEnvironment(),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
