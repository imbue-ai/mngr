"""The app's one in-workspace command for updates: install, check, arm, start, cancel, and read.

Every update action the app takes on a workspace is one ``mngr exec`` of one program (the
dispatcher's core from ``update_dispatcher_script`` plus the modes below), shipped base64 through
the shell and run with the workspace's own ``python3`` from its root. Each mode is idempotent and
prints one marker-wrapped JSON answer:

- ``arm``: install the dispatcher and the cron line, check the update-self skill, cron, and a
  signed-in account, then write the schedule file with the ceiling, the window (in UTC, from the
  user's local hours), and any version the user typed;
- ``now``: the same installs and checks (cron is not needed), then create the update chat with
  the auto-open label and return once its create has finished;
- ``cancel``: delete the schedule file;
- ``read``: the schedule file, the run record and the last dispatch, the detection sweep's one
  read; given the app's current values, an armed file is first refreshed with them, and asked,
  it says whether an account is signed in;
- ``tick``: the dispatcher's own minute, run by cron from the installed copy.

A program run by ``arm``, ``now`` or a refreshing ``read`` copies itself into the workspace, so
the dispatcher cron runs is always the code of the last app that touched it.
"""

import base64
import shlex
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from datetime import tzinfo
from enum import auto
from typing import Final
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.backup_workspace_scripts import extract_marker_json
from imbue.minds.desktop_client.in_workspace_mngr import build_in_workspace_command
from imbue.minds.desktop_client.in_workspace_mngr import exec_log_detail
from imbue.minds.desktop_client.in_workspace_mngr import exec_verdict_detail
from imbue.minds.desktop_client.in_workspace_mngr import inner_stdout_from_exec_result
from imbue.minds.desktop_client.in_workspace_mngr import is_exec_cut_off_on_reached_host
from imbue.minds.desktop_client.in_workspace_mngr import jsonl_events
from imbue.minds.desktop_client.update_dispatcher_script import UPDATE_COMMAND_CHECK_SIGN_IN_FLAG
from imbue.minds.desktop_client.update_dispatcher_script import UPDATE_COMMAND_RESULT_MARKER
from imbue.minds.desktop_client.update_dispatcher_script import UPDATE_DISPATCHER_PROGRAM_CORE
from imbue.minds.desktop_client.update_status import UpdateArmOutcome
from imbue.minds.desktop_client.update_status import UpdateLastDispatch
from imbue.minds.desktop_client.update_status import UpdateRunStatus
from imbue.minds.desktop_client.update_status import UpdateScheduleRecord
from imbue.minds.desktop_client.update_status import parse_update_epoch
from imbue.minds.desktop_client.update_status import parse_update_last_dispatch
from imbue.minds.desktop_client.update_status import parse_update_run_record
from imbue.minds.desktop_client.update_status import parse_update_schedule_record
from imbue.minds.utils.mngr_caller import MngrCaller

# The command's modes; ``tick`` is cron's alone.
_ARM_MODE: Final[str] = "arm"
_NOW_MODE: Final[str] = "now"
_CANCEL_MODE: Final[str] = "cancel"
_READ_MODE: Final[str] = "read"

# ``now`` waits for the chat app to finish the create (its own ceiling is 300s), after a listing
# of the workspace's agents; ``arm`` may ask an old template's account resolver under ``uv run``,
# and so may a ``read`` that checks the sign-in.
_NOW_TIMEOUT_SECONDS: Final[float] = 420.0
_ARM_TIMEOUT_SECONDS: Final[float] = 150.0
_READ_TIMEOUT_SECONDS: Final[float] = _ARM_TIMEOUT_SECONDS
_CANCEL_TIMEOUT_SECONDS: Final[float] = 60.0


class UtcUpdateWindow(FrozenModel):
    """The daily update window as the workspace's clock acts on it: a UTC start and a length."""

    start_minute_utc: int = Field(ge=0, lt=24 * 60, description="Minutes past UTC midnight the window opens at")
    length_minutes: int = Field(ge=0, lt=24 * 60, description="How long the window stays open")


def resolve_update_window_zone(zone_name: str) -> tzinfo:
    """The zone the user's window hours are read in: ``zone_name``, else the host's current offset."""
    if zone_name:
        try:
            return ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError) as e:
            logger.warning("Read the update window in the host's offset, as zone {!r} is unknown: {}", zone_name, e)
    host_zone = datetime.now().astimezone().tzinfo
    assert host_zone is not None
    return host_zone


@pure
def utc_update_window(start_hour: int, end_hour: int, zone: tzinfo, now: datetime) -> UtcUpdateWindow:
    """The local ``[start_hour, end_hour)`` window in ``zone``, as UTC.

    The UTC start is taken from the window open at ``now``, else the next to open, so it holds until
    the zone's offset next changes; the app refreshes it on every read of the workspace.
    """
    length = timedelta(hours=(end_hour - start_hour) % 24)
    local_today = now.astimezone(zone).date()
    starts = (
        datetime(day.year, day.month, day.day, start_hour, tzinfo=zone)
        for day in (local_today - timedelta(days=1), local_today, local_today + timedelta(days=1))
    )
    opened_at_utc = next(start for start in starts if now < start + length).astimezone(timezone.utc)
    return UtcUpdateWindow(
        start_minute_utc=opened_at_utc.hour * 60 + opened_at_utc.minute,
        length_minutes=int(length.total_seconds()) // 60,
    )


class UpdateWorkspaceSettings(FrozenModel):
    """The app's own values a command carries into the workspace."""

    ceiling_ref: str = Field(description="This app's release ceiling (``default_workspace_template_ref``)")
    window: UtcUpdateWindow = Field(description="The update window, in UTC")
    is_backup_configured: bool = Field(description="Whether the workspace has backups to fall back on")


class UpdateCommandReach(UpperCaseStrEnum):
    """How far one run of the command got."""

    ANSWERED = auto()
    """The program ran and answered."""
    NOT_REACHED = auto()
    """mngr never reached the workspace (host down, stopped, unreachable); nothing ran there."""
    CUT_OFF = auto()
    """The exec reached the workspace and was cut off, so the program may have run."""
    NO_ANSWER = auto()
    """mngr gave no account at all (it timed out or died), so whether anything ran is unknown."""


class UpdateCommandCall(FrozenModel):
    """One run of the command: how far it got, its answer when it gave one, and mngr's words when it did not."""

    reach: UpdateCommandReach = Field(description="How far the run got")
    payload: dict[str, object] | None = Field(description="The program's JSON answer; None unless ANSWERED")
    failure_detail: str = Field(
        default="", description="What mngr or the workspace said about a run that did not answer, bounded"
    )


class UpdateCommandResult(FrozenModel):
    """What an ``arm`` or ``now`` came to, in the program's words."""

    outcome: UpdateArmOutcome | None = Field(
        description="The program's outcome; None for one this build does not know (a newer program's)"
    )
    detail: str = Field(default="", description="The program's one line about it")
    chat_agent_name: str = Field(default="", description="The update chat a CREATED or ALREADY_RUNNING names")
    chat_id: str = Field(default="", description="That chat's id, for opening it; '' when unknown")
    run_started_at: datetime | None = Field(
        default=None,
        description="When an ALREADY_RUNNING run began (its record's start, or its chat's dispatch); None when unsaid",
    )
    schedule: UpdateScheduleRecord | None = Field(
        default=None, description="The schedule file as the command left it (``arm`` only)"
    )


class UpdateWorkspaceFacts(FrozenModel):
    """Everything the detection sweep reads from one workspace about its updates, in one ``read``."""

    schedule: UpdateScheduleRecord | None = Field(description="The armed schedule; None when none is armed")
    run_status: UpdateRunStatus | None = Field(description="The latest run's record; None when there is none")
    last_dispatch: UpdateLastDispatch | None = Field(description="The update chat the program last created")
    is_signed_in: bool | None = Field(
        description="Whether an account is signed in, asked only when the app's row or the schedule's skip says "
        "it was not; None when not asked"
    )


def build_update_workspace_command(mode: str, arguments: Sequence[str]) -> str:
    """The shell string that writes the program to a scratch file, runs it in ``mode``, and removes it.

    The program travels base64-encoded (no shell-significant characters, so single-quoting is
    safe) and runs from a file rather than stdin so that it can copy itself in as the
    dispatcher. Arguments are shell-quoted individually.
    """
    encoded = base64.b64encode(UPDATE_WORKSPACE_PROGRAM.encode("utf-8")).decode("ascii")
    run = f'{build_in_workspace_command(["python3"])} "$minds_update_program" {shlex.join([mode, *arguments])}'
    return (
        "minds_update_program=$(mktemp) && "
        f"printf %s '{encoded}' | base64 -d > \"$minds_update_program\" && "
        f'{run}; minds_update_exit=$?; rm -f "$minds_update_program"; exit $minds_update_exit'
    )


def _settings_arguments(settings: UpdateWorkspaceSettings) -> list[str]:
    return [
        "--ceiling",
        settings.ceiling_ref,
        "--window-start-minute-utc",
        str(settings.window.start_minute_utc),
        "--window-length-minutes",
        str(settings.window.length_minutes),
        "--backup-configured",
        "1" if settings.is_backup_configured else "0",
    ]


def build_arm_arguments(settings: UpdateWorkspaceSettings, override_ref: str, *, is_auto_armed: bool) -> list[str]:
    return [*_settings_arguments(settings), "--override", override_ref, "--auto-armed", "1" if is_auto_armed else "0"]


def build_read_arguments(settings: UpdateWorkspaceSettings, *, is_sign_in_checked: bool) -> list[str]:
    return [*_settings_arguments(settings), *([UPDATE_COMMAND_CHECK_SIGN_IN_FLAG] if is_sign_in_checked else [])]


def build_now_arguments(settings: UpdateWorkspaceSettings, override_ref: str) -> list[str]:
    return [
        "--ceiling",
        settings.ceiling_ref,
        "--override",
        override_ref,
        "--backup-configured",
        "1" if settings.is_backup_configured else "0",
    ]


def run_update_workspace_command(
    mngr_caller: MngrCaller, workspace_address: str, mode: str, arguments: Sequence[str], timeout: float
) -> UpdateCommandCall:
    """Run the command in ``mode`` on ``workspace_address`` and say how far it got.

    ``--no-start``: no command boots a stopped workspace; NOT_REACHED is the caller's cue that it
    may start the workspace and run again, since nothing ran.
    """
    command = build_update_workspace_command(mode, arguments)
    result = mngr_caller.call(
        ["exec", "--agent", workspace_address, command, "--no-start", "--format", "jsonl"], timeout=timeout
    )
    payload = extract_marker_json(inner_stdout_from_exec_result(result.stdout), UPDATE_COMMAND_RESULT_MARKER)
    if payload is not None:
        return UpdateCommandCall(reach=UpdateCommandReach.ANSWERED, payload=payload)
    if not result.is_mngr_output:
        logger.warning(
            "The update command ({}) in {} got no answer from mngr: {}", mode, workspace_address, result.stderr
        )
        return UpdateCommandCall(reach=UpdateCommandReach.NO_ANSWER, payload=None)
    detail = exec_verdict_detail(result)
    is_run_in_workspace = any(event.get("event") == "exec_result" for event in jsonl_events(result.stdout))
    if is_run_in_workspace or is_exec_cut_off_on_reached_host(result.stdout):
        # It reached the workspace and gave no answer: cut off, or the program died on the way.
        logger.warning(
            "The update command ({}) in {} gave no answer (exit {}): {}",
            mode,
            workspace_address,
            result.returncode,
            exec_log_detail(result),
        )
        return UpdateCommandCall(reach=UpdateCommandReach.CUT_OFF, payload=None, failure_detail=detail)
    logger.info("The update command ({}) never reached {}: {}", mode, workspace_address, detail)
    return UpdateCommandCall(reach=UpdateCommandReach.NOT_REACHED, payload=None, failure_detail=detail)


def _payload_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def parse_update_command_result(payload: Mapping[str, object]) -> UpdateCommandResult:
    """An ``arm`` or ``now`` answer as the app reads it; an outcome this build does not know reads as None."""
    raw_outcome = _payload_str(payload, "outcome")
    try:
        outcome: UpdateArmOutcome | None = UpdateArmOutcome(raw_outcome)
    except ValueError:
        logger.warning("The update command answered an outcome this build does not know: {!r}", raw_outcome)
        outcome = None
    raw_schedule = payload.get("schedule")
    return UpdateCommandResult(
        outcome=outcome,
        detail=_payload_str(payload, "detail"),
        chat_agent_name=_payload_str(payload, "chat_agent_name"),
        chat_id=_payload_str(payload, "chat_id"),
        run_started_at=parse_update_epoch(payload.get("run_started_at")),
        schedule=parse_update_schedule_record(raw_schedule) if raw_schedule is not None else None,
    )


def parse_update_workspace_facts(payload: Mapping[str, object]) -> UpdateWorkspaceFacts:
    """A ``read`` answer as the app reads it; each part is None when absent or unreadable."""
    raw_schedule = payload.get("schedule")
    raw_run = payload.get("run")
    raw_dispatch = payload.get("last_dispatch")
    is_signed_in = payload.get("is_signed_in")
    return UpdateWorkspaceFacts(
        schedule=parse_update_schedule_record(raw_schedule) if raw_schedule is not None else None,
        run_status=parse_update_run_record(raw_run) if raw_run is not None else None,
        last_dispatch=parse_update_last_dispatch(raw_dispatch) if raw_dispatch is not None else None,
        is_signed_in=is_signed_in if isinstance(is_signed_in, bool) else None,
    )


def arm_update(
    mngr_caller: MngrCaller,
    workspace_address: str,
    settings: UpdateWorkspaceSettings,
    override_ref: str,
    *,
    is_auto_armed: bool,
) -> UpdateCommandCall:
    return run_update_workspace_command(
        mngr_caller,
        workspace_address,
        _ARM_MODE,
        build_arm_arguments(settings, override_ref, is_auto_armed=is_auto_armed),
        _ARM_TIMEOUT_SECONDS,
    )


def start_update_now(
    mngr_caller: MngrCaller, workspace_address: str, settings: UpdateWorkspaceSettings, override_ref: str
) -> UpdateCommandCall:
    return run_update_workspace_command(
        mngr_caller, workspace_address, _NOW_MODE, build_now_arguments(settings, override_ref), _NOW_TIMEOUT_SECONDS
    )


def cancel_update_schedule(mngr_caller: MngrCaller, workspace_address: str) -> UpdateCommandCall:
    return run_update_workspace_command(mngr_caller, workspace_address, _CANCEL_MODE, (), _CANCEL_TIMEOUT_SECONDS)


def read_update_facts(
    mngr_caller: MngrCaller, workspace_address: str, settings: UpdateWorkspaceSettings, *, is_sign_in_checked: bool
) -> UpdateCommandCall:
    """The sweep's read; an armed schedule is refreshed with ``settings`` on the way.

    ``is_sign_in_checked`` asks whether an account is signed in, for a row a refusal says has none.
    """
    return run_update_workspace_command(
        mngr_caller,
        workspace_address,
        _READ_MODE,
        build_read_arguments(settings, is_sign_in_checked=is_sign_in_checked),
        _READ_TIMEOUT_SECONDS,
    )


# The app's modes, after the core they call into. A plain string for the same reason as the core.
_COMMAND_PROGRAM_SOURCE: Final[str] = r'''def cron_line(workspace_root):
    """The drop-in cron runs the dispatcher from: its own PATH and working directory, one tick at a time."""
    tick = "cd %s && python3 %s tick >> %s 2>&1" % (workspace_root, DISPATCHER_PATH, LOG_PATH)
    return "%s\n* * * * * root flock -n %s -c '%s'\n" % (CRON_PATH_LINE, CRON_LOCK_PATH, tick)


def install_dispatcher(context):
    """Copy this program into the workspace as its dispatcher, so it is the app's current code."""
    with open(context["program_path"], encoding="utf-8") as handle:
        source = handle.read()
    if write_text_if_changed(DISPATCHER_PATH, source):
        log("installed the dispatcher")


def install_cron_entry(context):
    """Write the cron line's durable copy (reinstalled at boot) and its live copy; whether cron will run it."""
    if not is_cron_available(context["live_cron_dir"]):
        return False
    line = cron_line(os.path.realpath(os.getcwd()))
    write_text_if_changed(DURABLE_CRON_PATH, line, mode=0o644)
    try:
        write_text_if_changed(os.path.join(context["live_cron_dir"], CRON_NAME), line, mode=0o644)
    except OSError as exc:
        log("could not install the live cron entry: %s" % exc)
        return False
    return True


def _arg(context, flag, default=""):
    argv = context["argv"]
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return argv[index + 1]
    return default


def _app_fields(context):
    """The schedule fields the app owns, from this run's arguments."""
    return {
        "ceiling": _arg(context, "--ceiling"),
        "window_start_minute_utc": int(_arg(context, "--window-start-minute-utc")),
        "window_length_minutes": int(_arg(context, "--window-length-minutes")),
        "is_backup_configured": _arg(context, "--backup-configured", "1") == "1",
    }


def _readiness_refusal(context, is_cron_needed):
    """``(refusal, account)``: the outcome that stops an arm or a now before it does anything (None to go
    ahead), and what ``resolve_account`` answered once the checks got that far (None before)."""
    if is_cron_needed and not is_cron_available(context["live_cron_dir"]):
        detail = "This workspace has no cron program, so nothing in it could start a scheduled update."
        return _create_result(OUTCOME_NO_CRON, detail), None
    if not is_skill_present():
        return _create_result(OUTCOME_TOO_OLD_TO_UPDATE, "This workspace predates the update-self skill."), None
    account = resolve_account()
    if not account[0]:
        return _create_result(OUTCOME_NEEDS_SIGN_IN, "No AI account is signed in to this workspace."), account
    return None, account


def mode_arm(context):
    install_dispatcher(context)
    refusal, _ = _readiness_refusal(context, is_cron_needed=True)
    if refusal is not None:
        emit(dict(refusal, mode="arm", schedule=read_json(SCHEDULE_PATH)))
        return
    if not install_cron_entry(context):
        detail = "This workspace's cron could not be given the update's schedule line."
        emit(dict(_create_result(OUTCOME_NO_CRON, detail), mode="arm", schedule=read_json(SCHEDULE_PATH)))
        return
    existing = read_json(SCHEDULE_PATH) or {}
    schedule = {
        "schema": SCHEMA,
        "armed_at": context["now"],
        "marked_chat": existing.get("marked_chat", ""),
        "marked_at": existing.get("marked_at"),
        "window_seen_open_at": existing.get("window_seen_open_at"),
        "pending_skip_reason": "",
        "next_attempt_at": None,
        "last_skip_reason": "",
        "last_skip_at": None,
    }
    schedule.update(_app_fields(context))
    schedule["override_ref"] = _arg(context, "--override")
    schedule["is_auto_armed"] = _arg(context, "--auto-armed", "0") == "1"
    write_json(SCHEDULE_PATH, schedule)
    log(
        "armed a schedule (%d minutes from %02d:%02d UTC, target %s)"
        % (
            schedule["window_length_minutes"],
            schedule["window_start_minute_utc"] // 60,
            schedule["window_start_minute_utc"] % 60,
            schedule["override_ref"] or schedule["ceiling"],
        )
    )
    emit(dict(_create_result(OUTCOME_ARMED), mode="arm", schedule=schedule))


def mode_now(context):
    install_dispatcher(context)
    # Only where cron runs the watchdog is a stopped lead brought back.
    is_watchdog_installed = install_cron_entry(context)
    refusal, account = _readiness_refusal(context, is_cron_needed=False)
    if refusal is not None:
        emit(dict(refusal, mode="now"))
        return
    agents = list_agents()
    run = read_json(RUN_PATH)
    # An ALREADY_RUNNING answer names when the run it found began, which tells its record apart from an
    # earlier run's under the same chat name.
    if is_run_in_flight(run, agents, is_stopped_lead_revived=is_watchdog_installed):
        lead = find_run_lead(agents or [], run)
        emit(
            dict(
                _create_result(
                    OUTCOME_ALREADY_RUNNING,
                    "An update is already running in this workspace.",
                    str(run["chat_agent_name"]),
                    chat_id_of(lead) if lead else "",
                ),
                mode="now",
                run_started_at=run_started_at(run),
            )
        )
        return
    if is_update_lease_held(agents):
        emit(
            dict(
                _create_result(OUTCOME_ALREADY_RUNNING, "Another update flow is working in this workspace."), mode="now"
            )
        )
        return
    pending = pending_dispatched_chat(run, agents, context["now"])
    if pending is not None:
        name, chat_id, dispatched_at = pending
        emit(
            dict(
                _create_result(
                    OUTCOME_ALREADY_RUNNING, "An update is already starting in this workspace.", name, chat_id
                ),
                mode="now",
                run_started_at=dispatched_at,
            )
        )
        return
    fields = {
        "ceiling": _arg(context, "--ceiling"),
        "override_ref": _arg(context, "--override"),
        "is_backup_configured": _arg(context, "--backup-configured", "1") == "1",
    }
    created = create_update_chat(
        fields, STARTED_BY_IMMEDIATE, is_auto_open=True, agents=agents, now=context["now"], account=account
    )
    emit(dict(created, mode="now"))


def mode_cancel(context):
    try:
        os.remove(SCHEDULE_PATH)
        is_cancelled = True
    except FileNotFoundError:
        is_cancelled = False
    if is_cancelled:
        log("cancelled the schedule")
    emit({"mode": "cancel", "is_cancelled": is_cancelled})


def mode_read(context):
    """The schedule, the run record and the last dispatch in one answer; with app fields given, an armed
    schedule is first rewritten with them and the dispatcher brought current. Whether an account is signed
    in rides along when the app asks, or the schedule's skip says none was."""
    schedule = read_json(SCHEDULE_PATH)
    if schedule is not None and _arg(context, "--ceiling"):
        fields = _app_fields(context)
        if any(schedule.get(key) != value for key, value in fields.items()):
            schedule.update(fields)
            write_json(SCHEDULE_PATH, schedule)
        install_dispatcher(context)
        install_cron_entry(context)
    is_signed_in = None
    is_skipped_for_sign_in = schedule is not None and SKIP_NEEDS_SIGN_IN in (
        schedule.get("pending_skip_reason"),
        schedule.get("last_skip_reason"),
    )
    if CHECK_SIGN_IN_FLAG in context["argv"] or is_skipped_for_sign_in:
        is_signed_in = resolve_account()[0]
    emit(
        {
            "mode": "read",
            "schedule": schedule,
            "run": read_json(RUN_PATH),
            "last_dispatch": read_json(LAST_DISPATCH_PATH),
            "is_signed_in": is_signed_in,
        }
    )


MODES = {"arm": mode_arm, "now": mode_now, "cancel": mode_cancel, "read": mode_read, "tick": mode_tick}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in MODES:
        sys.stderr.write("usage: %s {%s} [options]\n" % (sys.argv[0], "|".join(sorted(MODES))))
        return 2
    context = {
        "argv": sys.argv[2:],
        "program_path": os.path.abspath(sys.argv[0]),
        "live_cron_dir": LIVE_CRON_DIR,
        "now": time.time(),
    }
    MODES[mode](context)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

UPDATE_WORKSPACE_PROGRAM: Final[str] = f"{UPDATE_DISPATCHER_PROGRAM_CORE}\n\n{_COMMAND_PROGRAM_SOURCE}"
