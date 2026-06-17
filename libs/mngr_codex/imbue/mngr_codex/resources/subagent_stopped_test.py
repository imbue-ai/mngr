"""Tests for subagent_stopped.sh, the codex SubagentStop hook.

The script deregisters a finished subagent by removing its ``agent_id`` file
under ``codex_subagents/`` and recomputes the ``active`` marker. Because codex
subagents run asynchronously, the SubagentStop may arrive before or after the
root's Stop; the recompute makes the order irrelevant. The tests pin: a stop
removes the agent's file, the marker clears once the root turn is also done, a
stop with the root still active keeps the marker, a missing/unknown agent_id is
a no-op, and loud failure on a missing state dir.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from imbue.mngr_codex.resources.testing import install_common_transcript_flush_stub
from imbue.mngr_codex.resources.testing import provision_commands_dir
from imbue.mngr_codex.resources.testing import run_codex_hook

_STOP_SCRIPT = "subagent_stopped.sh"
_START_SCRIPT = "subagent_started.sh"
_SCRIPTS = (_STOP_SCRIPT, _START_SCRIPT)

_ROOT_SESSION = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_AGENT_A = "11111111-1111-1111-1111-111111111111"
_AGENT_B = "22222222-2222-2222-2222-222222222222"


def _payload(agent_id: str | None, event_name: str) -> str:
    fields = [f'"session_id":"{_ROOT_SESSION}"']
    if agent_id is not None:
        fields.append(f'"agent_id":"{agent_id}"')
    fields.append('"agent_type":"general"')
    fields.append(f'"hook_event_name":"{event_name}"')
    return "{" + ",".join(fields) + "}"


def _stop(state_dir: Path, agent_id: str | None) -> subprocess.CompletedProcess[str]:
    return run_codex_hook(state_dir, _STOP_SCRIPT, _payload(agent_id, "SubagentStop"))


def _start(state_dir: Path, agent_id: str) -> None:
    run_codex_hook(state_dir, _START_SCRIPT, _payload(agent_id, "SubagentStart"))


def _marker(state_dir: Path) -> Path:
    return state_dir / "active"


def _root_active(state_dir: Path) -> Path:
    return state_dir / "codex_root_active"


def _subagent_file(state_dir: Path, agent_id: str) -> Path:
    return state_dir / "codex_subagents" / agent_id


def test_stop_removes_agent_file_and_clears_marker_when_root_done(tmp_path: Path) -> None:
    """With the root turn already done, the last subagent's stop clears the marker."""
    provision_commands_dir(tmp_path, _SCRIPTS)
    _start(tmp_path, _AGENT_A)
    assert _marker(tmp_path).exists()
    result = _stop(tmp_path, _AGENT_A)
    assert not _subagent_file(tmp_path, _AGENT_A).exists()
    assert not _marker(tmp_path).exists()
    # Stop-class hooks must stay silent.
    assert result.stdout == ""


def test_stop_keeps_marker_while_root_active(tmp_path: Path) -> None:
    """If the root turn is still active, a subagent's stop must not clear the marker."""
    provision_commands_dir(tmp_path, _SCRIPTS)
    _start(tmp_path, _AGENT_A)
    _root_active(tmp_path).touch()
    _stop(tmp_path, _AGENT_A)
    assert _marker(tmp_path).exists()


def test_stop_keeps_marker_while_other_subagent_in_flight(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, _SCRIPTS)
    _start(tmp_path, _AGENT_A)
    _start(tmp_path, _AGENT_B)
    _stop(tmp_path, _AGENT_A)
    assert _marker(tmp_path).exists()


def test_last_subagent_stop_flushes_common_transcript_when_waiting(tmp_path: Path) -> None:
    """When the last subagent stops and the root turn is done (marker clears),
    the hook runs the turn-end common-transcript flush."""
    provision_commands_dir(tmp_path, _SCRIPTS)
    _start(tmp_path, _AGENT_A)
    sentinel = tmp_path / "flush_ran"
    install_common_transcript_flush_stub(tmp_path, sentinel)

    _stop(tmp_path, _AGENT_A)

    assert not _marker(tmp_path).exists()
    assert sentinel.exists(), "turn-end flush must run once the agent goes WAITING"


def test_subagent_stop_skips_flush_while_root_active(tmp_path: Path) -> None:
    """If the root turn is still active after a subagent stops (marker stays), the
    flush must not run."""
    provision_commands_dir(tmp_path, _SCRIPTS)
    _start(tmp_path, _AGENT_A)
    _root_active(tmp_path).touch()
    sentinel = tmp_path / "flush_ran"
    install_common_transcript_flush_stub(tmp_path, sentinel)

    _stop(tmp_path, _AGENT_A)

    assert _marker(tmp_path).exists()
    assert not sentinel.exists(), "flush must not run while the agent is still RUNNING"


def test_unknown_agent_id_is_a_noop(tmp_path: Path) -> None:
    """Stopping an agent that was never registered leaves the other agent intact."""
    provision_commands_dir(tmp_path, _SCRIPTS)
    _start(tmp_path, _AGENT_A)
    _stop(tmp_path, _AGENT_B)
    assert _subagent_file(tmp_path, _AGENT_A).exists()
    assert _marker(tmp_path).exists()


def test_missing_agent_id_is_a_noop(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, _SCRIPTS)
    _start(tmp_path, _AGENT_A)
    _stop(tmp_path, None)
    assert _subagent_file(tmp_path, _AGENT_A).exists()
    assert _marker(tmp_path).exists()


def test_missing_state_dir_fails_loudly(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, _SCRIPTS)
    result = subprocess.run(
        ["bash", str(tmp_path / "commands" / _STOP_SCRIPT)],
        input=_payload(_AGENT_A, "SubagentStop"),
        env={k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "MNGR_AGENT_STATE_DIR" in result.stderr
