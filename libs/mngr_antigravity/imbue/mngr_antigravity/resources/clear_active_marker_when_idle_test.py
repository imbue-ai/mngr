"""Tests for clear_active_marker_when_idle.sh, the Stop hook idle gate.

The script removes the per-agent ``active`` marker only when agy's Stop-hook
payload reports ``"fullyIdle":true``. The tests pin clear-on-fully-idle, keep
on an interim ``"fullyIdle":false`` and on an absent field (defensive),
whitespace tolerance, stdout silence (agy can treat Stop-hook stdout as a
stop-blocking result), garbage-stdin tolerance, and loud failure on a missing
state dir.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT_PATH = Path(__file__).parent / "clear_active_marker_when_idle.sh"

_CONV = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _payload(*, fully_idle: bool | None) -> str:
    """A Stop-hook payload shaped like the real one (verified live, agy 1.0.5).

    agy emits ``fullyIdle`` explicitly (interim Stop ``false``, final ``true``).
    ``fully_idle=None`` drops the field to exercise the defensive path.
    """
    brain = f"/home/u/.gemini/antigravity-cli/brain/{_CONV}"
    fields = [
        f'"artifactDirectoryPath":"{brain}"',
        f'"conversationId":"{_CONV}"',
        '"executionNum":0',
    ]
    if fully_idle is not None:
        fields.append(f'"fullyIdle":{"true" if fully_idle else "false"}')
    fields.append('"terminationReason":"DONE"')
    fields.append(f'"transcriptPath":"{brain}/.system_generated/logs/transcript.jsonl"')
    fields.append('"workspacePaths":["/tmp/ws"]')
    return "{" + ",".join(fields) + "}"


def _run(state_dir: Path, payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT_PATH)],
        input=payload,
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        check=True,
    )


def _marker(state_dir: Path) -> Path:
    return state_dir / "active"


def test_fully_idle_clears_the_marker(tmp_path: Path) -> None:
    _marker(tmp_path).touch()
    result = _run(tmp_path, _payload(fully_idle=True))
    assert not _marker(tmp_path).exists()
    # Stop handlers must stay silent: stdout is a structured result that can
    # block the stop.
    assert result.stdout == ""


def test_explicit_false_keeps_the_marker(tmp_path: Path) -> None:
    """An interim Stop sends ``"fullyIdle":false`` -> agent stays RUNNING.

    This is the form agy actually emits while a subagent / background task is
    still running (verified live against agy 1.0.5: a backgrounded shell task
    produced a ``fullyIdle:false`` Stop followed by a ``fullyIdle:true`` one).
    """
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(fully_idle=False))
    assert _marker(tmp_path).exists()


def test_absent_field_keeps_the_marker(tmp_path: Path) -> None:
    """A payload missing fullyIdle keeps the marker (defensive, never WAITING-early)."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(fully_idle=None))
    assert _marker(tmp_path).exists()


def test_pretty_printed_whitespace_still_matches(tmp_path: Path) -> None:
    """Whitespace between key, colon, and value does not defeat the match."""
    _marker(tmp_path).touch()
    _run(tmp_path, '{\n  "fullyIdle": true\n}')
    assert not _marker(tmp_path).exists()


def test_missing_marker_on_fully_idle_is_a_noop(tmp_path: Path) -> None:
    """Clearing an already-absent marker succeeds quietly (rm -f)."""
    result = _run(tmp_path, _payload(fully_idle=True))
    assert not _marker(tmp_path).exists()
    assert result.returncode == 0


def test_garbage_stdin_keeps_the_marker(tmp_path: Path) -> None:
    """Non-JSON stdin matches nothing, so the marker is preserved (never disrupts agy)."""
    _marker(tmp_path).touch()
    result = _run(tmp_path, "not json at all\n")
    assert _marker(tmp_path).exists()
    assert result.stdout == ""


def test_missing_state_dir_fails_loudly(tmp_path: Path) -> None:
    """An unset MNGR_AGENT_STATE_DIR is a wiring error: fail loudly (stderr,
    non-zero exit), never silently remove a marker at the filesystem root. Keep
    stdout empty so the Stop hook still emits no result.
    """
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH)],
        input=_payload(fully_idle=True),
        env={k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "MNGR_AGENT_STATE_DIR" in result.stderr
