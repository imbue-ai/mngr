"""The workspace dispatcher: its window, its dispatch decisions, its watchdog, and whole cron ticks."""

from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest

from imbue.minds.desktop_client.testing import ScriptedChatAppAnswer
from imbue.minds.desktop_client.testing import StagedUpdateWorkspace
from imbue.minds.desktop_client.testing import load_update_workspace_program
from imbue.minds.desktop_client.testing import scripted_update_chat_app
from imbue.minds.desktop_client.testing import stage_update_workspace
from imbue.minds.desktop_client.testing import update_program_context

_LOS_ANGELES = "America/Los_Angeles"
_LEAD = "update-to-minds-v0-9-0"
_LEAD_ID = "agent-" + "1" * 32
_WORKER = "update-self"
_CREATED = ScriptedChatAppAnswer(
    status=201, body={"chat_id": "agent-" + "a" * 32, "name": _LEAD, "display_name": "Update to v0.9.0"}
)


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")


@pytest.fixture
def program(tmp_path: Path) -> ModuleType:
    return load_update_workspace_program(tmp_path)


@pytest.mark.parametrize(
    ("now", "window", "zone", "opened_at_utc"),
    [
        # Inside a plain window, in the workspace user's zone rather than the clock's.
        ("2026-10-20T02:30:00-07:00", (2, 5), _LOS_ANGELES, "2026-10-20T09:00"),
        ("2026-10-20T11:30:00+02:00", (11, 12), "Europe/Berlin", "2026-10-20T09:00"),
        # A window that wraps midnight, read after midnight, opened the evening before.
        ("2026-10-21T01:00:00-07:00", (23, 3), _LOS_ANGELES, "2026-10-21T06:00"),
        # The spring-forward night has no 2 AM: the window opens at the first instant after the gap.
        ("2026-03-08T03:30:00-07:00", (2, 5), _LOS_ANGELES, "2026-03-08T10:00"),
        # The fall-back night has two 1 AMs: the window opened at the first.
        ("2026-11-01T01:30:00-08:00", (1, 3), _LOS_ANGELES, "2026-11-01T08:00"),
    ],
)
@pytest.mark.witnesses("workspace-updates.window-in-the-users-zone")
def test_the_window_opens_at_the_start_hour_in_the_files_zone(
    program: ModuleType, now: str, window: tuple[int, int], zone: str, opened_at_utc: str
) -> None:
    bounds = program.current_window(_epoch(now), window[0], window[1], zone)

    assert bounds is not None
    assert _utc(bounds[0]) == opened_at_utc


@pytest.mark.parametrize(
    ("now", "window"),
    [
        ("2026-10-20T05:00:00-07:00", (2, 5)),
        ("2026-10-20T01:59:00-07:00", (2, 5)),
        ("2026-10-20T12:00:00-07:00", (23, 3)),
    ],
)
def test_outside_the_window_there_is_no_window(program: ModuleType, now: str, window: tuple[int, int]) -> None:
    assert program.current_window(_epoch(now), window[0], window[1], _LOS_ANGELES) is None


_IN_WINDOW = _epoch("2026-10-20T03:00:00-07:00")
_WINDOW_OPENED = _epoch("2026-10-20T02:00:00-07:00")
_AFTER_WINDOW = _epoch("2026-10-20T06:00:00-07:00")


def _schedule(**fields: Any) -> dict[str, Any]:
    return {
        "armed_at": _WINDOW_OPENED - 86400,
        "ceiling": "minds-v0.9.0",
        "window_start_hour": 2,
        "window_end_hour": 5,
        "zone": _LOS_ANGELES,
        "override_ref": "",
        "marked_chat": "",
        "marked_at": None,
        "window_seen_open_at": None,
        "pending_skip_reason": "",
        **fields,
    }


def _never_gone() -> bool:
    raise AssertionError("the decision should not have asked whether the marked lead is gone")


@pytest.mark.parametrize(
    ("schedule", "run", "now", "is_gone", "action"),
    [
        # No run yet this window: start one.
        (_schedule(), None, _IN_WINDOW, _never_gone, "dispatch"),
        # A run that started before this window opened does not count against it.
        (
            _schedule(),
            {"chat_agent_name": "old", "started_at": _WINDOW_OPENED - 3600},
            _IN_WINDOW,
            _never_gone,
            "dispatch",
        ),
        # A run started since the window opened is this window's run.
        (
            _schedule(),
            {"chat_agent_name": "x", "started_at": _WINDOW_OPENED + 60},
            _IN_WINDOW,
            _never_gone,
            "note_window",
        ),
        # A chat dispatched this window whose lead never recorded a start, and is gone: start again.
        (_schedule(marked_chat=_LEAD, marked_at=_WINDOW_OPENED + 60), None, _IN_WINDOW, lambda: True, "dispatch"),
        # The same, with its lead still there: leave it be.
        (_schedule(marked_chat=_LEAD, marked_at=_WINDOW_OPENED + 60), None, _IN_WINDOW, lambda: False, "note_window"),
        # The run the file dispatched has a verdict: retire the file.
        (
            _schedule(marked_chat=_LEAD, marked_at=_WINDOW_OPENED + 60),
            {"chat_agent_name": _LEAD, "started_at": _WINDOW_OPENED - 99999, "verdict": "UPDATED"},
            _IN_WINDOW,
            _never_gone,
            "remove",
        ),
        # A verdict from a run that predates the arm says nothing about this schedule.
        (
            _schedule(),
            {"chat_agent_name": "old", "started_at": _WINDOW_OPENED - 99999 * 2, "verdict": "UPDATED"},
            _IN_WINDOW,
            _never_gone,
            "dispatch",
        ),
        # A window that closed after being seen open, with nothing started in it: record why.
        (_schedule(window_seen_open_at=_WINDOW_OPENED), None, _AFTER_WINDOW, _never_gone, "record_skip"),
        # One that closed with this window's run started in it: nothing to record.
        (
            _schedule(window_seen_open_at=_WINDOW_OPENED),
            {"chat_agent_name": "x", "started_at": _WINDOW_OPENED + 60},
            _AFTER_WINDOW,
            _never_gone,
            "note_window",
        ),
        # A workspace stopped through the whole window never saw it open, so nothing is recorded.
        (_schedule(), None, _AFTER_WINDOW, _never_gone, "nothing"),
    ],
)
@pytest.mark.witnesses("workspace-updates.gone-agent-restarted-in-window")
@pytest.mark.witnesses(
    "workspace-updates.stopped-workspace-not-updated", partial="the decision only; not the row once readable"
)
def test_one_tick_decides_from_the_window_and_the_run_record(
    program: ModuleType, schedule: dict[str, Any], run: dict[str, Any] | None, now: float, is_gone: Any, action: str
) -> None:
    assert program.decide_schedule_action(schedule, run, now, is_gone) == action


def _agent(name: str, state: str, agent_id: str = "") -> dict[str, Any]:
    return {"name": name, "state": state, "id": agent_id or f"agent-{uuid4().hex}", "labels": {}}


_OPEN_RUN = {"chat_agent_name": _LEAD, "started_at": _IN_WINDOW, "worker_agent_name": _WORKER}


@pytest.mark.parametrize(
    ("run", "agents", "busy", "state", "action"),
    [
        # The lead is gone with no verdict: close the run out.
        (_OPEN_RUN, [], None, {}, "close_out"),
        (_OPEN_RUN, [_agent(_LEAD, "DONE")], None, {}, "close_out"),
        # A listing that could not be read is no evidence of anything.
        (_OPEN_RUN, None, None, {}, "nothing"),
        # The worker is moving: nothing is owed yet.
        (_OPEN_RUN, [_agent(_LEAD, "WAITING"), _agent(_WORKER, "RUNNING")], None, {}, "reset"),
        # The lead waits on a background task the chat app reports busy.
        (_OPEN_RUN, [_agent(_LEAD, "WAITING"), _agent(_WORKER, "WAITING")], {_LEAD: True}, {}, "reset"),
        # Both idle, briefly: count it.
        (
            _OPEN_RUN,
            [_agent(_LEAD, "WAITING"), _agent(_WORKER, "WAITING")],
            {_LEAD: False, _WORKER: False},
            {},
            "count_idle",
        ),
        # Both idle for long enough, and not reminded lately: remind.
        (
            _OPEN_RUN,
            [_agent(_LEAD, "WAITING")],
            None,
            {"idle_ticks": 2, "last_reminded_at": 0.0},
            "remind",
        ),
        # Reminded a minute ago: wait out the interval.
        (
            _OPEN_RUN,
            [_agent(_LEAD, "WAITING")],
            None,
            {"idle_ticks": 9, "last_reminded_at": _IN_WINDOW - 60},
            "count_idle",
        ),
        # A run that is landing belongs to the apply's own recovery.
        ({**_OPEN_RUN, "apply_phase": "merged"}, [], None, {}, "reset"),
        # A record holding for the user (an older template's hold) is waiting on purpose.
        ({**_OPEN_RUN, "is_holding": True}, [_agent(_LEAD, "WAITING")], None, {"idle_ticks": 9}, "reset"),
        # A run with a verdict owes nothing.
        ({**_OPEN_RUN, "verdict": "UPDATED"}, [], None, {}, "nothing"),
    ],
)
def test_the_watchdog_decides_from_the_lead_and_worker_states(
    program: ModuleType,
    run: dict[str, Any],
    agents: list[dict[str, Any]] | None,
    busy: dict[str, bool] | None,
    state: dict[str, Any],
    action: str,
) -> None:
    assert program.decide_watchdog_action(run, agents, busy, state, _IN_WINDOW) == action


def _tick(program: ModuleType, staged: StagedUpdateWorkspace, now: float) -> None:
    program.mode_tick(update_program_context(program, staged, [], now))


@pytest.mark.witnesses("workspace-updates.runs-without-the-app")
def test_a_tick_inside_the_window_starts_the_run_once_and_marks_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: ModuleType
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state("schedule.json", _schedule())
    with scripted_update_chat_app(staged.root, create_answers=[_CREATED, _CREATED]) as requests:
        _tick(program, staged, _IN_WINDOW)
        staged.set_agents([_agent(_LEAD, "RUNNING", "agent-" + "a" * 32)])
        _tick(program, staged, _IN_WINDOW + 60)

    creates = [request.body for request in requests if request.path == "/api/chats/create"]
    assert len(creates) == 1
    assert "labels" in creates[0] and creates[0]["labels"] == {}
    schedule = staged.read_state("schedule.json")
    assert schedule is not None
    assert (schedule["marked_chat"], schedule["marked_at"]) == (_LEAD, _IN_WINDOW)
    dispatch = staged.read_state("last-dispatch.json")
    assert dispatch is not None and dispatch["started_by"] == "scheduled"


@pytest.mark.witnesses("workspace-updates.skipped-window", partial="the recorded reason; not the modal line")
def test_a_tick_that_cannot_start_the_run_keeps_trying_and_records_why_at_window_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: ModuleType
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state("schedule.json", _schedule())
    tickets = staged.root / "data" / ".tickets"
    tickets.mkdir(parents=True)
    (tickets / "w-1.md").write_text("---\nid: w-1\nstatus: in_progress\n---\n# updating workspace\n")

    _tick(program, staged, _IN_WINDOW)
    in_window = staged.read_state("schedule.json")
    _tick(program, staged, _AFTER_WINDOW)

    assert in_window is not None and in_window["pending_skip_reason"] == "LEASE_HELD"
    closed = staged.read_state("schedule.json")
    assert closed is not None
    assert (closed["last_skip_reason"], closed["pending_skip_reason"]) == ("LEASE_HELD", "")
    assert closed["last_skip_at"] == _AFTER_WINDOW


@pytest.mark.witnesses("workspace-updates.needs-sign-in", partial="the dispatcher's skip only")
def test_a_sign_in_refusal_at_dispatch_time_is_the_windows_skip_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: ModuleType
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch, is_signed_in=False)
    staged.write_state("schedule.json", _schedule())

    _tick(program, staged, _IN_WINDOW)

    schedule = staged.read_state("schedule.json")
    assert schedule is not None and schedule["pending_skip_reason"] == "NEEDS_SIGN_IN"


@pytest.mark.witnesses("workspace-updates.failure-cancels-the-schedule")
def test_a_tick_retires_the_schedule_once_its_run_has_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: ModuleType
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state("schedule.json", _schedule(marked_chat=_LEAD, marked_at=_IN_WINDOW))
    staged.write_state("run.json", {"chat_agent_name": _LEAD, "started_at": _IN_WINDOW + 30, "verdict": "UPDATED"})

    _tick(program, staged, _IN_WINDOW + 3600)

    assert staged.read_state("schedule.json") is None


@pytest.mark.witnesses("workspace-updates.gone-agent-closed-out")
def test_the_watchdog_closes_out_a_run_whose_lead_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: ModuleType
) -> None:
    """Recorded STUCK, the worker stopped (its branch kept), and only the gone lead's leases released."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state("run.json", {**_OPEN_RUN, "verdict": None})
    staged.set_agents([_agent(_WORKER, "RUNNING")])
    tickets = staged.root / "data" / ".tickets"
    tickets.mkdir(parents=True)
    (tickets / "w-1.md").write_text(
        f"---\nid: w-1\nstatus: in_progress\n---\n# updating workspace\n\nHeld by {_LEAD} across the update-self pass.\n"
    )
    (tickets / "w-2.md").write_text(
        "---\nid: w-2\nstatus: in_progress\nagent: someone-else\n---\n# updating workspace\n"
    )

    with scripted_update_chat_app(staged.root):
        _tick(program, staged, _IN_WINDOW)

    run = staged.read_state("run.json")
    assert run is not None
    assert run["verdict"] == "STUCK"
    calls = staged.recorded_calls()
    assert ["stop", _WORKER] in calls
    closes = [call for call in calls if call[:1] == ["close"]]
    assert [call[1] for call in closes] == ["w-1"]


@pytest.mark.witnesses("workspace-updates.idle-agent-reminded")
def test_the_watchdog_reminds_an_idle_lead_through_its_chat_and_then_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: ModuleType
) -> None:
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state("run.json", {"chat_agent_name": _LEAD, "started_at": _IN_WINDOW})
    staged.set_agents([_agent(_LEAD, "WAITING", _LEAD_ID)])

    with scripted_update_chat_app(staged.root, tracked_agents=[{"name": _LEAD, "is_busy": False}]):
        for minute in range(5):
            _tick(program, staged, _IN_WINDOW + 60 * minute)

    messages = [call for call in staged.recorded_calls() if call[:1] == ["message"]]
    assert len(messages) == 1
    assert messages[0][1] == _LEAD
    assert "no recorded result yet" in messages[0][3]


def test_a_failed_create_is_not_retried_until_its_backoff_has_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: ModuleType
) -> None:
    """A create that failed may have left a chat behind; retrying every minute for the whole window would pile them up."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state("schedule.json", _schedule())
    failure = ScriptedChatAppAnswer(status=500, body={"detail": "mngr create exited 1"})
    with scripted_update_chat_app(staged.root, create_answers=[failure, _CREATED]) as requests:
        _tick(program, staged, _IN_WINDOW)
        _tick(program, staged, _IN_WINDOW + 60)
        _tick(program, staged, _IN_WINDOW + 16 * 60)

    assert len([request for request in requests if request.path == "/api/chats/create"]) == 2
    schedule = staged.read_state("schedule.json")
    assert schedule is not None and schedule["marked_chat"] == _LEAD


def test_a_listing_without_the_services_agent_never_closes_a_run_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, program: ModuleType
) -> None:
    """Such a listing read the wrong host dir; it is no evidence that the run's agent is gone."""
    staged = stage_update_workspace(tmp_path, monkeypatch)
    staged.write_state("run.json", {"chat_agent_name": _LEAD, "started_at": _IN_WINDOW})
    staged.set_agents([], is_services_agent_listed=False)

    with scripted_update_chat_app(staged.root):
        _tick(program, staged, _IN_WINDOW)

    run = staged.read_state("run.json")
    assert run is not None and run.get("verdict") is None
