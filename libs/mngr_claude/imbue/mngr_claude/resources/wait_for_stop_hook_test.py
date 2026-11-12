import importlib.resources
import json
import os
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

import pytest

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr import resources as mngr_resources
from imbue.mngr.hosts.work_leases import DEFAULT_WORK_LEASE_TTL_SECONDS
from imbue.mngr.hosts.work_leases import WORK_LEASE_TTL_ENV_VAR
from imbue.mngr.hosts.work_leases import parse_work_leases
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_claude.resources.testing import make_assistant_record
from imbue.mngr_claude.resources.testing import make_user_record
from imbue.mngr_claude.resources.testing import write_raw_transcript

_WAIT_FOR_STOP_HOOK_PATH: Final[Path] = Path(__file__).parent / "wait_for_stop_hook.sh"
_WORK_LEASE_LIB_NAME: Final[str] = "mngr_work_lease_lib.sh"
_HOOK_TIMEOUT_SECONDS: Final[float] = 30.0
# Long enough that the race test can append a record while the hook waits to re-check
_SLOW_REQUEUE_RECHECK_DELAY_SECONDS: Final[float] = 2.0
# How much later than the test's own clock the hook may date a lease from
_LEASE_TTL_SLACK_SECONDS: Final[float] = 30.0


class _StopHook(FrozenModel):
    """One prepared stop-hook invocation and the files it reads and writes."""

    hook_input: str
    env: dict[str, str]
    transcript: Path
    active_marker: Path
    idle_since: Path
    activity_events: Path
    lease_dir: Path


def _queue_operation(operation: str, content: str = "", timestamp: str = "2026-01-01T00:00:03.000Z") -> dict[str, Any]:
    return {"type": "queue-operation", "operation": operation, "timestamp": timestamp, "content": content}


def _stop_hook_summary() -> dict[str, Any]:
    return {"type": "system", "subtype": "stop_hook_summary", "hookCount": 1, "uuid": uuid4().hex}


def _prompt(text: str) -> dict[str, Any]:
    return make_user_record(uuid4().hex, text=text)


def _reply(text: str) -> dict[str, Any]:
    return make_assistant_record(uuid4().hex, text=text)


def _stop_hook_feedback() -> dict[str, Any]:
    # The record Claude Code writes as soon as a stop hook exits 2, shaped as Claude Code 2.1.305 writes it
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": "Stop hook feedback:\n[check_repo_root.sh]: Be sure to return to the repo root when you finish!\n",
        },
        "isMeta": True,
        "uuid": uuid4().hex,
    }


def _api_error(timestamp: str) -> dict[str, Any]:
    record = make_assistant_record(uuid4().hex, text="API Error: 529 Overloaded.", timestamp=timestamp)
    return {**record, "isApiErrorMessage": True, "error": "server_error"}


def _queued_prompt_dequeued_ahead_of_api_error(content: str) -> list[dict[str, Any]]:
    # Claude Code dequeues the next prompt as soon as the StopFailure hooks start,
    # writing the dequeue ahead of the older API-error record
    return [
        _queue_operation("enqueue", content, timestamp="2026-01-01T00:00:03.000Z"),
        _queue_operation("dequeue", timestamp="2026-01-01T00:00:05.031Z"),
        _api_error(timestamp="2026-01-01T00:00:05.000Z"),
    ]


def _install_work_lease_lib(agent_state_dir: Path) -> None:
    """Stage the shared work-lease library where the hook sources it from.

    Host._ensure_shared_shell_libs writes it into the agent's ``commands/`` dir at
    provisioning time; this is that one file, so the hook under test declares leases the
    way it does on a real agent rather than against a stand-in.
    """
    commands_dir = agent_state_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    (commands_dir / _WORK_LEASE_LIB_NAME).write_text(
        importlib.resources.files(mngr_resources).joinpath(_WORK_LEASE_LIB_NAME).read_text()
    )


def _prepare_stop_hook(
    tmp_path: Path,
    transcript_records: list[dict[str, Any]] | None,
    hook_event_name: str = "Stop",
    requeue_recheck_delay_seconds: float = 0.0,
    background_tasks: list[dict[str, Any]] | None = None,
    is_work_lease_lib_installed: bool = True,
    work_lease_ttl_override: str = "",
) -> _StopHook:
    """Set up the stop hook for the end of a turn that set the ``active`` marker.

    ``transcript_records`` is the session transcript the hook input points at; None
    sends no ``transcript_path``, as claude versions without the field do.

    ``is_work_lease_lib_installed`` stages the shared work-lease library into the agent's
    ``commands/`` dir the way ``Host._ensure_shared_shell_libs`` does, which is the state
    every provisioned agent is in. Clearing it reproduces an agent provisioned before the
    library existed.

    ``work_lease_ttl_override`` is the agent's setting for how long its leases hold; empty
    is the agent that sets none, whatever the test runner's own environment holds.
    """
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (agent_state_dir / "active").touch()
    if is_work_lease_lib_installed:
        _install_work_lease_lib(agent_state_dir)
    transcript = tmp_path / "session.jsonl"
    hook_input: dict[str, Any] = {"hook_event_name": hook_event_name, "session_id": uuid4().hex}
    if transcript_records is not None:
        write_raw_transcript(transcript, transcript_records)
        hook_input["transcript_path"] = str(transcript)
    if background_tasks is not None:
        hook_input["background_tasks"] = background_tasks
    return _StopHook(
        hook_input=json.dumps(hook_input),
        env={
            **os.environ,
            "MAIN_CLAUDE_SESSION_ID": uuid4().hex,
            "MNGR_AGENT_STATE_DIR": str(agent_state_dir),
            "MNGR_HOST_DIR": str(host_dir),
            "HOOK_GRACE_PERIOD": "0",
            "HOOK_MAX_WAIT": "0",
            "HOOK_REQUEUE_RECHECK_DELAY": str(requeue_recheck_delay_seconds),
            WORK_LEASE_TTL_ENV_VAR: work_lease_ttl_override,
        },
        transcript=transcript,
        active_marker=agent_state_dir / "active",
        idle_since=agent_state_dir / "idle_since",
        activity_events=host_dir / "events" / "mngr" / "activity" / "events.jsonl",
        lease_dir=agent_state_dir / "leases",
    )


def _run_prepared_stop_hook(
    hook: _StopHook,
    tmp_path: Path,
    hook_input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the hook against an agent state dir the caller has already arranged.

    ``hook_input`` replaces the prepared payload, for a test that adds a field to it.
    """
    return subprocess.run(
        ["bash", str(_WAIT_FOR_STOP_HOOK_PATH)],
        input=hook.hook_input if hook_input is None else hook_input,
        capture_output=True,
        text=True,
        timeout=_HOOK_TIMEOUT_SECONDS,
        env=hook.env,
        cwd=tmp_path,
    )


def _run_stop_hook_at_turn_end(
    tmp_path: Path,
    transcript_records: list[dict[str, Any]] | None,
    hook_event_name: str = "Stop",
    background_tasks: list[dict[str, Any]] | None = None,
    is_work_lease_lib_installed: bool = True,
    work_lease_ttl_override: str = "",
) -> _StopHook:
    hook = _prepare_stop_hook(
        tmp_path,
        transcript_records,
        hook_event_name,
        background_tasks=background_tasks,
        is_work_lease_lib_installed=is_work_lease_lib_installed,
        work_lease_ttl_override=work_lease_ttl_override,
    )
    result = _run_prepared_stop_hook(hook, tmp_path)
    assert result.returncode == 0, result.stderr
    return hook


def _assert_left_active(hook: _StopHook) -> None:
    assert hook.active_marker.exists()
    assert not hook.idle_since.exists()
    assert not hook.activity_events.exists()


def _assert_marked_idle(hook: _StopHook) -> None:
    assert not hook.active_marker.exists()
    assert hook.idle_since.exists()
    assert len(hook.activity_events.read_text().splitlines()) == 1


@pytest.mark.parametrize(
    "transcript_records",
    [
        pytest.param(
            [
                _stop_hook_summary(),
                _prompt("/welcome"),
                _queue_operation("enqueue", "what's on my reddit front page?"),
            ],
            id="prompt_queued",
        ),
        pytest.param(
            [_prompt("hello"), _queue_operation("enqueue", "next"), _queue_operation("enqueue", "/clear")],
            id="prompt_queued_ahead_of_a_slash_command",
        ),
    ],
)
def test_stop_hook_leaves_agent_active_when_a_queued_prompt_starts_the_next_turn(
    tmp_path: Path, transcript_records: list[dict[str, Any]]
) -> None:
    _assert_left_active(_run_stop_hook_at_turn_end(tmp_path, transcript_records))


@pytest.mark.parametrize(
    "transcript_records",
    [
        pytest.param([_prompt("hello")], id="no_queued_prompt"),
        pytest.param(
            [_prompt("hello"), _queue_operation("enqueue", "also this"), _queue_operation("remove", "also this")],
            id="queued_prompt_injected_into_this_turn",
        ),
        pytest.param(
            [_prompt("hello"), _queue_operation("enqueue", "next"), _stop_hook_summary(), _queue_operation("dequeue")],
            id="queued_prompt_dequeued_into_this_turn",
        ),
        pytest.param(
            [_queue_operation("enqueue", "stranded"), _stop_hook_summary(), _prompt("hello")],
            id="enqueue_stranded_by_an_earlier_turn",
        ),
        pytest.param(
            [_prompt("hello"), _queue_operation("enqueue", "next"), _queue_operation("popAll", "next")],
            id="queued_prompt_pulled_back_into_the_editor",
        ),
        pytest.param([_prompt("hello"), _queue_operation("enqueue", "/clear")], id="slash_command_queued"),
        pytest.param(
            [_prompt("hello"), _queue_operation("enqueue", "/compact keep the plan")],
            id="slash_command_queued_with_args",
        ),
        pytest.param(None, id="no_transcript_path"),
    ],
)
def test_stop_hook_marks_agent_idle_when_no_prompt_is_queued(
    tmp_path: Path, transcript_records: list[dict[str, Any]] | None
) -> None:
    _assert_marked_idle(_run_stop_hook_at_turn_end(tmp_path, transcript_records))


def test_stop_hook_leaves_agent_active_when_another_stop_hook_blocked_the_turn_end(tmp_path: Path) -> None:
    transcript_records = [_prompt("hello"), _reply("done"), _stop_hook_feedback()]

    _assert_left_active(_run_stop_hook_at_turn_end(tmp_path, transcript_records))


@pytest.mark.parametrize(
    "transcript_records",
    [
        pytest.param(
            [
                _prompt("hello"),
                _reply("done"),
                _stop_hook_feedback(),
                _stop_hook_summary(),
                _reply("back at the root"),
            ],
            id="block_answered_by_the_continuation_that_is_now_ending",
        ),
        pytest.param(
            [_prompt("hello"), _stop_hook_feedback(), _stop_hook_summary(), _prompt("next"), _reply("ok")],
            id="block_from_an_earlier_turn",
        ),
        pytest.param([_prompt("Stop hook feedback: is this a block?"), _reply("no")], id="prompt_quoting_the_prefix"),
    ],
)
def test_stop_hook_marks_agent_idle_when_no_stop_hook_blocked_this_turn_end(
    tmp_path: Path, transcript_records: list[dict[str, Any]]
) -> None:
    _assert_marked_idle(_run_stop_hook_at_turn_end(tmp_path, transcript_records))


def test_stop_failure_hook_marks_agent_idle_even_after_a_block(tmp_path: Path) -> None:
    transcript_records = [_prompt("hello"), _stop_hook_feedback(), _api_error(timestamp="2026-01-01T00:00:05.000Z")]

    _assert_marked_idle(_run_stop_hook_at_turn_end(tmp_path, transcript_records, hook_event_name="StopFailure"))


def test_stop_failure_hook_leaves_agent_active_when_the_queued_prompt_already_started(tmp_path: Path) -> None:
    transcript_records = [_prompt("hello"), *_queued_prompt_dequeued_ahead_of_api_error("next")]

    _assert_left_active(_run_stop_hook_at_turn_end(tmp_path, transcript_records, hook_event_name="StopFailure"))


@pytest.mark.parametrize(
    "transcript_records",
    [
        pytest.param([_prompt("hello"), _api_error(timestamp="2026-01-01T00:00:05.000Z")], id="no_queued_prompt"),
        pytest.param(
            [_prompt("hello"), *_queued_prompt_dequeued_ahead_of_api_error("/clear")], id="slash_command_queued"
        ),
        pytest.param(
            [
                _queue_operation("enqueue", "next", timestamp="2026-01-01T00:00:03.000Z"),
                _stop_hook_summary(),
                _queue_operation("dequeue", timestamp="2026-01-01T00:00:04.000Z"),
                _api_error(timestamp="2026-01-01T00:00:05.000Z"),
            ],
            id="failed_turn_was_the_queued_prompt",
        ),
    ],
)
def test_stop_failure_hook_marks_agent_idle_when_no_prompt_was_queued(
    tmp_path: Path, transcript_records: list[dict[str, Any]]
) -> None:
    _assert_marked_idle(_run_stop_hook_at_turn_end(tmp_path, transcript_records, hook_event_name="StopFailure"))


def test_stop_hook_marks_agent_idle_when_the_turn_a_stop_failure_started_ends(tmp_path: Path) -> None:
    transcript_records = [_prompt("hello"), *_queued_prompt_dequeued_ahead_of_api_error("next"), _prompt("next")]

    _assert_marked_idle(_run_stop_hook_at_turn_end(tmp_path, transcript_records))


def test_stop_hook_restores_the_marker_when_the_enqueue_lands_after_the_clear(tmp_path: Path) -> None:
    hook = _prepare_stop_hook(
        tmp_path, [_prompt("hello")], requeue_recheck_delay_seconds=_SLOW_REQUEUE_RECHECK_DELAY_SECONDS
    )
    with subprocess.Popen(
        ["bash", str(_WAIT_FOR_STOP_HOOK_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=hook.env,
        cwd=tmp_path,
    ) as process:
        assert process.stdin is not None
        process.stdin.write(hook.hook_input)
        process.stdin.close()

        # UserPromptSubmit touched the marker just before the clear removed it; the
        # prompt's enqueue record reaches the transcript only afterwards
        assert poll_until(lambda: not hook.active_marker.exists(), timeout=_HOOK_TIMEOUT_SECONDS, poll_interval=0.01)
        with hook.transcript.open("a") as transcript:
            transcript.write(json.dumps(_queue_operation("enqueue", "sent just before the clear")) + "\n")
        process.wait(timeout=_HOOK_TIMEOUT_SECONDS)
        assert process.stderr is not None
        stderr = process.stderr.read()
    assert process.returncode == 0, stderr

    _assert_left_active(hook)


def test_stop_hook_leases_the_background_work_the_turn_leaves_running(tmp_path: Path) -> None:
    hook = _run_stop_hook_at_turn_end(
        tmp_path,
        [_prompt("hello"), _reply("done")],
        background_tasks=[{"task_id": "abc-123"}, {"task_id": "def/456"}],
    )

    leases = sorted(path.name for path in hook.lease_dir.iterdir())
    assert leases == ["task-abc-123", "task-def_456"]


def test_stop_hook_writes_a_lease_mngr_can_parse_and_date(tmp_path: Path) -> None:
    """The grammar the shared library writes: a bound, so the lease can expire, and a start.

    Parsed here by the reader that consumes it in production, so the two halves of the
    protocol are pinned against each other rather than against a literal spelled twice.
    """
    hook = _run_stop_hook_at_turn_end(
        tmp_path,
        [_prompt("hello"), _reply("done")],
        background_tasks=[{"task_id": "abc-123"}],
    )

    lease_path = hook.lease_dir / "task-abc-123"
    leases = parse_work_leases(f"{lease_path.name}\t{lease_path.read_text().strip()}")

    assert len(leases) == 1
    assert leases[0].name == "task-abc-123"
    assert leases[0].expires_at is not None
    assert leases[0].expires_at > datetime.now(timezone.utc)


def _lease_ttl_seconds_at_turn_end(tmp_path: Path, work_lease_ttl_override: str) -> float:
    """How far ahead the hook dates a lease, measured from the test's clock before it ran."""
    started_at = datetime.now(timezone.utc)
    hook = _run_stop_hook_at_turn_end(
        tmp_path,
        [_prompt("hello"), _reply("done")],
        background_tasks=[{"task_id": "abc-123"}],
        work_lease_ttl_override=work_lease_ttl_override,
    )
    lease_path = hook.lease_dir / "task-abc-123"
    leases = parse_work_leases(f"{lease_path.name}\t{lease_path.read_text().strip()}")
    assert len(leases) == 1
    expires_at = leases[0].expires_at
    assert expires_at is not None
    return (expires_at - started_at).total_seconds()


def _assert_lease_ttl_is(observed_ttl_seconds: float, expected_ttl_seconds: int) -> None:
    """The hook dates a lease from its own clock, truncated to a whole second."""
    assert expected_ttl_seconds - 1 <= observed_ttl_seconds <= expected_ttl_seconds + _LEASE_TTL_SLACK_SECONDS, (
        observed_ttl_seconds
    )


def test_stop_hook_dates_a_lease_by_the_default_ttl(tmp_path: Path) -> None:
    """The bound an agent that sets nothing gets, which is the one mngr documents for every writer."""
    _assert_lease_ttl_is(_lease_ttl_seconds_at_turn_end(tmp_path, ""), DEFAULT_WORK_LEASE_TTL_SECONDS)


def test_stop_hook_dates_a_lease_by_the_agents_own_ttl(tmp_path: Path) -> None:
    """An agent whose work is short can say so, rather than holding its host for the default."""
    _assert_lease_ttl_is(_lease_ttl_seconds_at_turn_end(tmp_path, "600"), 600)


@pytest.mark.parametrize("override", ["", " ", "0", "007", "4h", "-600", "1.5"])
def test_stop_hook_ignores_a_lease_ttl_that_is_not_a_positive_count_of_seconds(tmp_path: Path, override: str) -> None:
    """A lease dated by one of these would be expired on arrival, which reads as work that ended."""
    _assert_lease_ttl_is(_lease_ttl_seconds_at_turn_end(tmp_path, override), DEFAULT_WORK_LEASE_TTL_SECONDS)


def test_stop_hook_leaves_the_turn_signal_alone_when_the_lease_library_is_missing(tmp_path: Path) -> None:
    """An agent provisioned before the library existed must still reach the end of its turn.

    The library is written at provisioning time, so an older agent simply has no writer. It
    declares no leases and loses nothing else: the marker still clears, which is what anything
    waiting on this agent is watching.
    """
    hook = _run_stop_hook_at_turn_end(
        tmp_path,
        [_prompt("hello"), _reply("done")],
        background_tasks=[{"task_id": "abc-123"}],
        is_work_lease_lib_installed=False,
    )

    assert not hook.lease_dir.exists()
    assert not hook.active_marker.exists()


def test_stop_hook_writes_no_leases_when_no_background_work_is_in_flight(tmp_path: Path) -> None:
    hook = _run_stop_hook_at_turn_end(tmp_path, [_prompt("hello"), _reply("done")], background_tasks=[])

    assert not hook.lease_dir.exists()


def test_stop_hook_drops_leases_for_work_that_has_since_finished(tmp_path: Path) -> None:
    hook = _prepare_stop_hook(tmp_path, [_prompt("hello"), _reply("done")], background_tasks=[])
    hook.lease_dir.mkdir()
    (hook.lease_dir / "task-stale").write_text("until:1\n")

    result = _run_prepared_stop_hook(hook, tmp_path)

    assert result.returncode == 0, result.stderr
    assert list(hook.lease_dir.iterdir()) == []


def test_stop_hook_leaves_alone_a_lease_it_did_not_write(tmp_path: Path) -> None:
    """The lease directory is the agent's, and anything that starts work may lease there.

    Each lease states a bound it expires by, so there is nothing for this hook to clean up
    on another writer's behalf -- and a lease it deleted anyway would vanish mid-work for
    no reason its writer could see.
    """
    hook = _prepare_stop_hook(tmp_path, [_prompt("hello"), _reply("done")], background_tasks=[])
    hook.lease_dir.mkdir()
    (hook.lease_dir / "task-stale").write_text("until:1\n")
    someone_elses = hook.lease_dir / "mngr-long-build"
    someone_elses.write_text("pid:4821\n")

    result = _run_prepared_stop_hook(hook, tmp_path)

    assert result.returncode == 0, result.stderr
    assert [path.name for path in hook.lease_dir.iterdir()] == ["mngr-long-build"]
    assert someone_elses.read_text() == "pid:4821\n"


def test_stop_hook_leases_every_kind_of_background_task_claude_reports(tmp_path: Path) -> None:
    """The entry shape Claude Code sends: the id field is `id`, and the kind is in `type`."""
    hook = _run_stop_hook_at_turn_end(
        tmp_path,
        [_prompt("hello"), _reply("done")],
        background_tasks=[
            {"id": "b3tzesn7m", "type": "shell", "status": "running", "description": "a background command"},
            {"id": "mon-1", "type": "monitor", "status": "running", "description": "an armed monitor"},
            {"id": "mcp-1", "type": "MCP task", "status": "running", "description": "an mcp call"},
            {"id": "wf-1", "type": "workflow", "status": "running", "description": "a workflow"},
            {"id": "sub-1", "type": "subagent", "status": "running", "description": "a backgrounded subagent"},
        ],
    )

    assert sorted(path.name for path in hook.lease_dir.iterdir()) == [
        "task-b3tzesn7m",
        "task-mcp-1",
        "task-mon-1",
        "task-sub-1",
        "task-wf-1",
    ]


def test_stop_hook_does_not_lease_a_schedule(tmp_path: Path) -> None:
    """A schedule is an appointment, not work in flight: nothing runs until it fires.

    Leasing one would hold its agent for as long as the schedule existed, which for a standing
    schedule is forever -- see specs/agent-running-background.
    """
    hook = _prepare_stop_hook(
        tmp_path,
        [_prompt("hello"), _reply("done")],
        background_tasks=[{"id": "bg-1", "type": "shell", "status": "running", "description": "a task"}],
    )
    hook_input = json.loads(hook.hook_input)
    hook_input["session_crons"] = [
        {"id": "dfc9c3e2", "schedule": "41 16 23 10 *", "recurring": False, "prompt": "say hi"},
        {"id": "hourly", "schedule": "0 * * * *", "recurring": True, "prompt": "poll"},
    ]

    result = _run_prepared_stop_hook(hook, tmp_path, hook_input=json.dumps(hook_input))

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in hook.lease_dir.iterdir()) == ["task-bg-1"]


def test_stop_hook_marks_the_turn_over_even_when_the_leases_cannot_be_recorded(tmp_path: Path) -> None:
    """Recording leases runs ahead of the marker clear, so it must not be able to stop it.

    The hook runs under `set -e`, and the agent's lease directory is ordinary state that
    anything may have left in a shape the hook cannot clear. Were that failure to abort
    the hook, the `active` marker would never be removed and the agent would read RUNNING
    for the rest of its life -- a worse outcome than recording no leases at all. A
    directory under a lease's own name is the shape no `rm -f` can clear, at any uid.
    """
    hook = _prepare_stop_hook(tmp_path, [_prompt("hello"), _reply("done")], background_tasks=[])
    (hook.lease_dir / "task-unclearable").mkdir(parents=True)

    result = _run_prepared_stop_hook(hook, tmp_path)

    assert result.returncode == 0, result.stderr
    _assert_marked_idle(hook)


def test_stop_hook_does_not_lease_a_task_that_reports_a_terminal_status(tmp_path: Path) -> None:
    """A finished task must not hold the agent open for a whole lease TTL.

    Claude Code documents `background_tasks` as in-flight work only, so this guards the case
    where that stops holding rather than one seen in a payload.
    """
    hook = _run_stop_hook_at_turn_end(
        tmp_path,
        [_prompt("hello"), _reply("done")],
        background_tasks=[
            {"id": "still-running", "status": "running"},
            {"id": "queued", "status": "pending"},
            {"id": "paused-task", "status": "paused"},
            {"id": "unknown-to-us"},
            {"id": "finished", "status": "completed"},
            {"id": "broke", "status": "failed"},
            {"id": "stopped", "status": "cancelled"},
            {"id": "slain", "status": "killed"},
        ],
    )

    assert sorted(path.name for path in hook.lease_dir.iterdir()) == [
        "task-paused-task",
        "task-queued",
        "task-still-running",
        "task-unknown-to-us",
    ]
