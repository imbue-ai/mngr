from __future__ import annotations

import importlib.resources
import json
import os
import subprocess
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr import resources as mngr_resources
from imbue.mngr.primitives import TurnEndKind
from imbue.mngr.utils.testing import read_turn_end_in
from imbue.mngr_antigravity.resources.testing import TURN_END_KIND_BY_TERMINATION_REASON
from imbue.mngr_antigravity.resources.testing import agent_env

_SCRIPT_PATH: Final[Path] = Path(__file__).parent / "turn_end_hook.sh"
_ROOT: Final[str] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_SUBAGENT: Final[str] = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_CLOCK_SLACK: Final[timedelta] = timedelta(seconds=5)


def _payload(event: str, conversation_id: str, **fields: object) -> str:
    """An agy hook payload shaped like agy 1.5.9's: compact, keys in alphabetical order."""
    brain = f"/home/u/.gemini/antigravity-cli/brain/{conversation_id}"
    common = {
        "artifactDirectoryPath": brain,
        "conversationId": conversation_id,
        "cwd": "/tmp/ws",
        "hookEventName": event,
        "lastUserInput": "Reply with the single word ok.",
        "modelName": "gemini-3.8-flash-high",
        "transcriptPath": f"{brain}/.system_generated/logs/transcript_full.jsonl",
        "workspacePaths": ["/tmp/ws"],
    }
    return json.dumps({**common, **fields}, sort_keys=True, separators=(",", ":"))


def _stop(conversation_id: str, termination_reason: str, **fields: object) -> str:
    stop_fields: dict[str, object] = {
        "continuationActive": False,
        "error": "",
        "executionNum": 0,
        "fullyIdle": True,
        "terminationReason": termination_reason,
    }
    return _payload("Stop", conversation_id, **{**stop_fields, **fields})


def _invocation(conversation_id: str) -> str:
    return _payload("PreInvocation", conversation_id, initialNumSteps=1, invocationNum=0)


def _make_state_dir(tmp_path: Path, *, is_root_known: bool = True, is_library_installed: bool = True) -> Path:
    """An agy agent's state dir after statusline.sh has recorded ``_ROOT`` as its root conversation."""
    state_dir = tmp_path / "state"
    (state_dir / "commands").mkdir(parents=True)
    if is_root_known:
        (state_dir / "root_conversation").write_text(_ROOT)
    if is_library_installed:
        library = importlib.resources.files(mngr_resources).joinpath("mngr_background_task_lib.sh").read_text()
        (state_dir / "commands" / "mngr_background_task_lib.sh").write_text(library)
    return state_dir


def _execute(event: str, payload: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT_PATH), event],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run(state_dir: Path, event: str, payload: str) -> None:
    result = _execute(event, payload, agent_env(state_dir))
    # agy reads PreInvocation stdout as injected steps and Stop stdout as a decision.
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.parametrize(("termination_reason", "kind"), sorted(TURN_END_KIND_BY_TERMINATION_REASON.items()))
def test_a_root_stop_records_its_termination_reason_as_the_turn_end(
    tmp_path: Path, termination_reason: str, kind: TurnEndKind
) -> None:
    state_dir = _make_state_dir(tmp_path)
    stopped_at = datetime.now(timezone.utc)

    _run(state_dir, "Stop", _stop(_ROOT, termination_reason))

    turn_end = read_turn_end_in(state_dir)
    assert turn_end is not None
    assert (turn_end.kind, turn_end.native_type) == (kind, termination_reason)
    assert abs(turn_end.ended_at - stopped_at) <= _CLOCK_SLACK


def test_a_root_stop_that_leaves_background_work_or_follows_a_stop_hook_continuation_still_ends_the_turn(
    tmp_path: Path,
) -> None:
    state_dir = _make_state_dir(tmp_path)

    _run(state_dir, "Stop", _stop(_ROOT, "NO_TOOL_CALL", fullyIdle=False, continuationActive=True, executionNum=1))

    turn_end = read_turn_end_in(state_dir)
    assert turn_end is not None
    assert turn_end.kind is TurnEndKind.COMPLETED


@pytest.mark.parametrize("termination_reason", ["UNSPECIFIED", "A_FUTURE_REASON", ""])
def test_a_root_stop_for_a_reason_with_no_kind_leaves_no_turn_end(tmp_path: Path, termination_reason: str) -> None:
    state_dir = _make_state_dir(tmp_path)
    _run(state_dir, "Stop", _stop(_ROOT, "ERROR"))

    _run(state_dir, "Stop", _stop(_ROOT, termination_reason))

    assert not (state_dir / "turn_end").exists()


@pytest.mark.parametrize(
    ("last_user_input", "termination_reason", "kind"),
    [
        ("/compact", "TERMINAL_CUSTOM_HOOK", None),
        ("/compact keep the API notes", "TERMINAL_CUSTOM_HOOK", None),
        ("/compact keep the API notes", "NO_TOOL_CALL", TurnEndKind.COMPLETED),
        ("/compaction-notes", "TERMINAL_CUSTOM_HOOK", TurnEndKind.INTERRUPTED),
        ("explain /compact", "TERMINAL_CUSTOM_HOOK", TurnEndKind.INTERRUPTED),
    ],
)
def test_only_the_end_agy_gives_a_compaction_leaves_no_turn_end(
    tmp_path: Path, last_user_input: str, termination_reason: str, kind: TurnEndKind | None
) -> None:
    state_dir = _make_state_dir(tmp_path)
    _run(state_dir, "Stop", _stop(_ROOT, "ERROR"))

    _run(state_dir, "Stop", _stop(_ROOT, termination_reason, lastUserInput=last_user_input))

    turn_end = read_turn_end_in(state_dir)
    assert (turn_end.kind if turn_end is not None else None) is kind


def test_a_prompt_quoting_a_termination_reason_does_not_change_the_one_recorded(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)

    _run(state_dir, "Stop", _stop(_ROOT, "NO_TOOL_CALL", lastUserInput='print {"terminationReason":"ERROR"}'))

    turn_end = read_turn_end_in(state_dir)
    assert turn_end is not None
    assert (turn_end.kind, turn_end.native_type) == (TurnEndKind.COMPLETED, "NO_TOOL_CALL")


def test_a_subagent_stop_leaves_the_root_turn_end_alone(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    _run(state_dir, "Stop", _stop(_ROOT, "NO_TOOL_CALL"))

    _run(state_dir, "Stop", _stop(_SUBAGENT, "ERROR"))

    turn_end = read_turn_end_in(state_dir)
    assert turn_end is not None
    assert turn_end.kind is TurnEndKind.COMPLETED


def test_a_root_invocation_removes_the_last_turn_end_and_a_subagent_invocation_does_not(tmp_path: Path) -> None:
    state_dir = _make_state_dir(tmp_path)
    _run(state_dir, "Stop", _stop(_ROOT, "NO_TOOL_CALL"))

    _run(state_dir, "PreInvocation", _invocation(_SUBAGENT))
    assert read_turn_end_in(state_dir) is not None

    _run(state_dir, "PreInvocation", _invocation(_ROOT))
    assert not (state_dir / "turn_end").exists()


@pytest.mark.parametrize(
    ("is_root_known", "is_library_installed"),
    [pytest.param(False, True, id="root_conversation_not_yet_recorded"), pytest.param(True, False, id="no_library")],
)
def test_a_root_stop_records_nothing_without_what_it_needs(
    tmp_path: Path, is_root_known: bool, is_library_installed: bool
) -> None:
    state_dir = _make_state_dir(tmp_path, is_root_known=is_root_known, is_library_installed=is_library_installed)

    _run(state_dir, "Stop", _stop(_ROOT, "NO_TOOL_CALL"))

    assert not (state_dir / "turn_end").exists()


@pytest.mark.parametrize(
    "state_dir_env", [pytest.param({}, id="unset"), pytest.param({"MNGR_AGENT_STATE_DIR": ""}, id="empty")]
)
def test_a_missing_state_dir_fails_loudly_without_printing_to_stdout(state_dir_env: dict[str, str]) -> None:
    env = {k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"} | state_dir_env

    result = _execute("Stop", _stop(_ROOT, "NO_TOOL_CALL"), env)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "MNGR_AGENT_STATE_DIR" in result.stderr
