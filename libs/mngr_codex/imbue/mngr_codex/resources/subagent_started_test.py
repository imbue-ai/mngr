"""Tests for subagent_started.sh, the codex SubagentStart hook.

The script registers an in-flight subagent by writing one empty file per
``agent_id`` under ``codex_subagents/`` and recomputes the ``active`` marker, so
the marker stays RUNNING while async subagents run. The tests pin: a start
registers the agent and keeps the marker present, a missing agent_id is a no-op
that still recomputes, repeated starts of the same agent are idempotent, and
loud failure on a missing state dir.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from imbue.mngr_codex.resources.testing import provision_commands_dir
from imbue.mngr_codex.resources.testing import run_codex_hook

_SCRIPT = "subagent_started.sh"
_ROOT_SESSION = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_AGENT_A = "11111111-1111-1111-1111-111111111111"


def _payload(agent_id: str | None) -> str:
    fields = [f'"session_id":"{_ROOT_SESSION}"']
    if agent_id is not None:
        fields.append(f'"agent_id":"{agent_id}"')
    fields.append('"agent_type":"general"')
    fields.append('"hook_event_name":"SubagentStart"')
    return "{" + ",".join(fields) + "}"


def _run(state_dir: Path, agent_id: str | None) -> subprocess.CompletedProcess[str]:
    return run_codex_hook(state_dir, _SCRIPT, _payload(agent_id))


def _marker(state_dir: Path) -> Path:
    return state_dir / "active"


def _subagent_file(state_dir: Path, agent_id: str) -> Path:
    return state_dir / "codex_subagents" / agent_id


def test_subagent_start_registers_agent_and_keeps_marker(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, (_SCRIPT,))
    # A subagent can start while the root marker is already present.
    _marker(tmp_path).touch()
    result = _run(tmp_path, _AGENT_A)
    assert _subagent_file(tmp_path, _AGENT_A).exists()
    assert _marker(tmp_path).exists()
    # Stop-class hooks must stay silent.
    assert result.stdout == ""


def test_subagent_start_recomputes_marker_when_root_inactive(tmp_path: Path) -> None:
    """Even with no root-turn flag, a registered subagent keeps the marker present
    (the invariant is root-active OR any subagent in flight)."""
    provision_commands_dir(tmp_path, (_SCRIPT,))
    _run(tmp_path, _AGENT_A)
    assert _marker(tmp_path).exists()


def test_missing_agent_id_is_a_noop(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, (_SCRIPT,))
    _run(tmp_path, None)
    subagents_dir = tmp_path / "codex_subagents"
    assert subagents_dir.exists()
    assert list(subagents_dir.iterdir()) == []
    assert not _marker(tmp_path).exists()


def test_repeated_start_is_idempotent(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, (_SCRIPT,))
    _run(tmp_path, _AGENT_A)
    _run(tmp_path, _AGENT_A)
    assert _subagent_file(tmp_path, _AGENT_A).exists()
    assert _marker(tmp_path).exists()


def test_missing_state_dir_fails_loudly(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, (_SCRIPT,))
    result = subprocess.run(
        ["bash", str(tmp_path / "commands" / _SCRIPT)],
        input=_payload(_AGENT_A),
        env={k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "MNGR_AGENT_STATE_DIR" in result.stderr
