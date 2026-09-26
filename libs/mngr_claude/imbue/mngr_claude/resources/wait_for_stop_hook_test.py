import json
import os
import subprocess
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

import pytest

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_claude.resources.testing import make_assistant_record
from imbue.mngr_claude.resources.testing import make_user_record
from imbue.mngr_claude.resources.testing import write_raw_transcript

_WAIT_FOR_STOP_HOOK_PATH: Final[Path] = Path(__file__).parent / "wait_for_stop_hook.sh"
_HOOK_TIMEOUT_SECONDS: Final[float] = 30.0
# Long enough that the race test can append a record while the hook waits to re-check
_SLOW_REQUEUE_RECHECK_DELAY_SECONDS: Final[float] = 2.0


class _StopHook(FrozenModel):
    """One prepared stop-hook invocation and the files it reads and writes."""

    hook_input: str
    env: dict[str, str]
    transcript: Path
    active_marker: Path
    idle_since: Path
    activity_events: Path


def _queue_operation(operation: str, content: str = "", timestamp: str = "2026-01-01T00:00:03.000Z") -> dict[str, Any]:
    return {"type": "queue-operation", "operation": operation, "timestamp": timestamp, "content": content}


def _stop_hook_summary() -> dict[str, Any]:
    return {"type": "system", "subtype": "stop_hook_summary", "hookCount": 1, "uuid": uuid4().hex}


def _prompt(text: str) -> dict[str, Any]:
    return make_user_record(uuid4().hex, text=text)


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


def _prepare_stop_hook(
    tmp_path: Path,
    transcript_records: list[dict[str, Any]] | None,
    hook_event_name: str = "Stop",
    requeue_recheck_delay_seconds: float = 0.0,
) -> _StopHook:
    """Set up the stop hook for the end of a turn that set the ``active`` marker.

    ``transcript_records`` is the session transcript the hook input points at; None
    sends no ``transcript_path``, as claude versions without the field do.
    """
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir()
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (agent_state_dir / "active").touch()
    transcript = tmp_path / "session.jsonl"
    hook_input: dict[str, Any] = {"hook_event_name": hook_event_name, "session_id": uuid4().hex}
    if transcript_records is not None:
        write_raw_transcript(transcript, transcript_records)
        hook_input["transcript_path"] = str(transcript)
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
        },
        transcript=transcript,
        active_marker=agent_state_dir / "active",
        idle_since=agent_state_dir / "idle_since",
        activity_events=host_dir / "events" / "mngr" / "activity" / "events.jsonl",
    )


def _run_stop_hook_at_turn_end(
    tmp_path: Path, transcript_records: list[dict[str, Any]] | None, hook_event_name: str = "Stop"
) -> _StopHook:
    hook = _prepare_stop_hook(tmp_path, transcript_records, hook_event_name)
    result = subprocess.run(
        ["bash", str(_WAIT_FOR_STOP_HOOK_PATH)],
        input=hook.hook_input,
        capture_output=True,
        text=True,
        timeout=_HOOK_TIMEOUT_SECONDS,
        env=hook.env,
        cwd=tmp_path,
    )
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
