"""Unit tests for ``codex_turn_hook.sh``, run with payloads shaped like codex-cli 0.163.0's."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.mngr_codex.codex_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_codex.codex_config import APP_SERVER_THREAD_FILENAME
from imbue.mngr_codex.codex_config import PERMISSIONS_WAITING_FILENAME

_SCRIPT: Final[Path] = Path(__file__).parent / "codex_turn_hook.sh"
_ROOT_THREAD: Final[str] = "01a1f3a9-e697-7321-b00a-cfd4a913958d"
_TURN: Final[str] = "01a1f3aa-2f4b-73f1-bd7d-8062a3beb1f2"


def _payload(event: str, session_id: str = _ROOT_THREAD, subagent: dict[str, str] | None = None, **fields: Any) -> str:
    """A hook payload as codex sends it: compact JSON, a subagent's fields after the turn id and the event's own last."""
    return json.dumps(
        {
            "session_id": session_id,
            "turn_id": _TURN,
            **(subagent or {}),
            "transcript_path": "/home/user/.codex/sessions/2026/11/19/rollout-x.jsonl",
            "cwd": "/home/user/work",
            "hook_event_name": event,
            "model": "gpt-6.1-sol",
            "permission_mode": "default",
            **fields,
        },
        separators=(",", ":"),
    )


_SUBAGENT: Final[dict[str, str]] = {"agent_id": "01a1f3ae-e22c-7c42-8577-c296824e1be7", "agent_type": "default"}
_APPROVAL_FIELDS: Final[dict[str, Any]] = {"tool_name": "Bash", "tool_input": {"command": "uv run sleep 20"}}
_QUESTION_FIELDS: Final[dict[str, Any]] = {
    "tool_name": "request_user_input",
    "tool_input": {
        "questions": [
            {
                "header": "Language",
                "id": "language",
                "question": "Which language should the greeting script use: Python or Bash?",
                "options": [{"label": "Python", "description": "A Python script."}],
            }
        ]
    },
}


def _state_dir(tmp_path: Path) -> Path:
    """An agent state dir that records ``_ROOT_THREAD`` as its root thread."""
    state_dir = tmp_path / "agent"
    state_dir.mkdir()
    (state_dir / APP_SERVER_THREAD_FILENAME).write_text(_ROOT_THREAD)
    return state_dir


def _run(state_dir: Path, event: str, payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), event],
        input=payload,
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        check=True,
    )


def _markers(state_dir: Path) -> tuple[str | None, bool]:
    active = state_dir / ACTIVE_MARKER_FILENAME
    return (active.read_text() if active.exists() else None, (state_dir / PERMISSIONS_WAITING_FILENAME).exists())


def test_a_root_prompt_opens_the_turn_under_its_turn_id(tmp_path: Path) -> None:
    state_dir = _state_dir(tmp_path)
    (state_dir / PERMISSIONS_WAITING_FILENAME).touch()

    result = _run(state_dir, "UserPromptSubmit", _payload("UserPromptSubmit", prompt="hello"))

    assert result.stdout == ""
    assert _markers(state_dir) == (f"{_TURN}\n", False)


@pytest.mark.parametrize(
    "stop_hook_active",
    [pytest.param(False, id="first_stop"), pytest.param(True, id="stop_after_another_hook_blocked")],
)
def test_a_root_stop_leaves_the_turn_open_and_removes_the_dialog_marker(
    stop_hook_active: bool, tmp_path: Path
) -> None:
    state_dir = _state_dir(tmp_path)
    (state_dir / ACTIVE_MARKER_FILENAME).write_text(f"{_TURN}\n")
    (state_dir / PERMISSIONS_WAITING_FILENAME).touch()

    _run(state_dir, "Stop", _payload("Stop", stop_hook_active=stop_hook_active, last_assistant_message="OK"))

    assert _markers(state_dir) == (f"{_TURN}\n", False)


def test_a_root_interrupt_removes_both_markers(tmp_path: Path) -> None:
    state_dir = _state_dir(tmp_path)
    (state_dir / ACTIVE_MARKER_FILENAME).write_text(f"{_TURN}\n")
    (state_dir / PERMISSIONS_WAITING_FILENAME).touch()

    _run(state_dir, "Interrupt", _payload("Interrupt"))

    assert _markers(state_dir) == (None, False)


def test_an_approval_holds_the_turn_until_its_tool_finishes(tmp_path: Path) -> None:
    state_dir = _state_dir(tmp_path)
    _run(state_dir, "UserPromptSubmit", _payload("UserPromptSubmit", prompt="run it"))
    _run(state_dir, "PermissionRequest", _payload("PermissionRequest", **_APPROVAL_FIELDS))
    held = _markers(state_dir)

    _run(state_dir, "PostToolUse", _payload("PostToolUse", **_APPROVAL_FIELDS, tool_response="", tool_use_id="exec-1"))

    assert held == (f"{_TURN}\n", True)
    assert _markers(state_dir) == (f"{_TURN}\n", False)


def test_a_question_to_the_user_holds_the_turn_until_it_is_answered(tmp_path: Path) -> None:
    state_dir = _state_dir(tmp_path)
    _run(state_dir, "UserPromptSubmit", _payload("UserPromptSubmit", prompt="plan it"))
    _run(state_dir, "PreToolUse", _payload("PreToolUse", **_QUESTION_FIELDS, tool_use_id="call-1"))
    held = _markers(state_dir)

    _run(
        state_dir,
        "PostToolUse",
        _payload("PostToolUse", **_QUESTION_FIELDS, tool_response='{"answers":{}}', tool_use_id="call-1"),
    )

    assert held == (f"{_TURN}\n", True)
    assert _markers(state_dir) == (f"{_TURN}\n", False)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_payload("PreToolUse", **_APPROVAL_FIELDS, tool_use_id="exec-1"), id="another_tool"),
        pytest.param(
            _payload("PreToolUse", subagent=_SUBAGENT, **_QUESTION_FIELDS, tool_use_id="call-1"),
            id="a_subagents_question",
        ),
    ],
)
def test_a_tool_start_other_than_a_root_question_leaves_the_dialog_marker_alone(payload: str, tmp_path: Path) -> None:
    state_dir = _state_dir(tmp_path)
    _run(state_dir, "UserPromptSubmit", _payload("UserPromptSubmit", prompt="run it"))

    _run(state_dir, "PreToolUse", payload)

    assert _markers(state_dir) == (f"{_TURN}\n", False)


def test_a_root_tool_whose_input_names_an_agent_still_counts(tmp_path: Path) -> None:
    state_dir = _state_dir(tmp_path)
    tool: dict[str, Any] = {
        "tool_name": "collaborationwait_agent",
        "tool_input": {"agent_id": "01a1f410-d16e-7e51-ab71-aba5201258ae"},
    }
    _run(state_dir, "UserPromptSubmit", _payload("UserPromptSubmit", prompt="wait for it"))
    _run(state_dir, "PermissionRequest", _payload("PermissionRequest", **tool))
    held = _markers(state_dir)

    _run(state_dir, "PostToolUse", _payload("PostToolUse", **tool, tool_response="", tool_use_id="call-2"))

    assert held == (f"{_TURN}\n", True)
    assert _markers(state_dir) == (f"{_TURN}\n", False)


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        pytest.param(
            "UserPromptSubmit", _payload("UserPromptSubmit", subagent=_SUBAGENT, prompt="hi"), id="subagent_prompt"
        ),
        pytest.param(
            "PermissionRequest",
            _payload("PermissionRequest", subagent=_SUBAGENT, **_APPROVAL_FIELDS),
            id="subagent_approval",
        ),
        pytest.param(
            "PostToolUse",
            _payload("PostToolUse", subagent=_SUBAGENT, **_APPROVAL_FIELDS, tool_response="", tool_use_id="exec-1"),
            id="subagent_tool_end",
        ),
        pytest.param("Stop", _payload("Stop", session_id="another-codex-session"), id="another_sessions_stop"),
        pytest.param(
            "UserPromptSubmit",
            _payload("UserPromptSubmit", session_id="another-codex-session", prompt="hi"),
            id="another_sessions_prompt",
        ),
    ],
)
def test_an_event_outside_the_root_thread_changes_nothing(event: str, payload: str, tmp_path: Path) -> None:
    state_dir = _state_dir(tmp_path)
    (state_dir / ACTIVE_MARKER_FILENAME).write_text("root-turn\n")
    (state_dir / PERMISSIONS_WAITING_FILENAME).touch()

    _run(state_dir, event, payload)

    assert _markers(state_dir) == ("root-turn\n", True)


def test_without_a_recorded_root_thread_any_session_is_the_root(tmp_path: Path) -> None:
    state_dir = _state_dir(tmp_path)
    (state_dir / APP_SERVER_THREAD_FILENAME).unlink()

    _run(state_dir, "UserPromptSubmit", _payload("UserPromptSubmit", session_id="fresh-thread", prompt="hi"))

    assert _markers(state_dir) == (f"{_TURN}\n", False)


@pytest.mark.parametrize(
    "event", ["UserPromptSubmit", "Stop", "Interrupt", "PermissionRequest", "PreToolUse", "PostToolUse"]
)
def test_every_event_exits_cleanly_when_the_state_dir_is_gone(event: str, tmp_path: Path) -> None:
    missing_state_dir = tmp_path / "destroyed-agent"

    result = _run(missing_state_dir, event, _payload(event, **_QUESTION_FIELDS))

    assert result.returncode == 0
    assert result.stdout == ""
    assert not missing_state_dir.exists()


def test_missing_state_dir_env_fails_loudly() -> None:
    result = subprocess.run(
        ["bash", str(_SCRIPT), "Stop"], input="{}", env={}, capture_output=True, text=True, check=False
    )

    assert result.returncode != 0
    assert "MNGR_AGENT_STATE_DIR" in result.stderr
