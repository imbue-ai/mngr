"""Unit tests for ``record_session_pointers.sh``.

The UserPromptSubmit hook records two non-lifecycle pointers from codex's stdin payload:
the rollout ``session_id`` into ``codex_root_session`` and the ``transcript_path`` into
``codex_transcript_path`` (which ``stream_transcript.sh`` tails). It is self-contained (no
sourced helper) and does no lifecycle-marker work.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).parent / "record_session_pointers.sh"


def _compact(payload: dict[str, str]) -> str:
    """Serialize like codex's hook wire format: compact JSON with no spaces after separators."""
    return json.dumps(payload, separators=(",", ":"))


def _run(state_dir: Path, payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        input=payload,
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        check=True,
    )


def test_records_session_id_and_transcript_path(tmp_path: Path) -> None:
    state_dir = tmp_path / "agent"
    state_dir.mkdir()
    rollout = "/home/user/.codex/sessions/2026/08/11/rollout-2026-08-11T00-00-00-abc.jsonl"
    payload = _compact({"session_id": "11111111-2222-3333-4444-555555555555", "transcript_path": rollout})

    result = _run(state_dir, payload)

    # The hook must never write stdout (codex treats UserPromptSubmit stdout as model context).
    assert result.stdout == ""
    assert (state_dir / "codex_root_session").read_text() == "11111111-2222-3333-4444-555555555555"
    assert (state_dir / "codex_transcript_path").read_text() == rollout


def test_records_transcript_path_with_spaces(tmp_path: Path) -> None:
    """A transcript path may contain spaces and slashes; the whole value is captured."""
    state_dir = tmp_path / "agent"
    state_dir.mkdir()
    rollout = "/home/a user/sessions/rollout-abc.jsonl"
    _run(state_dir, _compact({"session_id": "abc", "transcript_path": rollout}))
    assert (state_dir / "codex_transcript_path").read_text() == rollout


def test_missing_fields_leave_files_unwritten(tmp_path: Path) -> None:
    """A payload with neither field writes nothing and still exits cleanly."""
    state_dir = tmp_path / "agent"
    state_dir.mkdir()
    _run(state_dir, _compact({"unrelated": "value"}))
    assert not (state_dir / "codex_root_session").exists()
    assert not (state_dir / "codex_transcript_path").exists()


def test_missing_state_dir_env_fails_loudly(tmp_path: Path) -> None:
    """The hook is a wiring error without MNGR_AGENT_STATE_DIR, so it exits non-zero."""
    result = subprocess.run(
        ["bash", str(_SCRIPT)],
        input="{}",
        env={},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "MNGR_AGENT_STATE_DIR" in result.stderr
