"""Tests for clear_active_marker_when_idle.sh, the Stop hook idle gate.

The script reads an agy Stop-hook payload (JSON) on stdin and removes the
per-agent ``active`` lifecycle marker only when the payload reports
``"fullyIdle":true`` -- i.e. when the root agent and every subagent /
background task it launched have all finished. The marker drives BaseAgent's
RUNNING/WAITING detection, so the tests pin: clear-on-fully-idle,
keep-while-not-idle (field omitted, as agy does for the proto's omitempty
bool), keep on an explicit ``false``, tolerance of pretty-printed whitespace,
stdout silence (agy treats Stop-hook stdout as a result that can block the
stop), tolerance of garbage stdin, and loud failure on a missing state dir.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT_PATH = Path(__file__).parent / "clear_active_marker_when_idle.sh"

_CONV = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _payload(*, fully_idle: bool | None) -> str:
    """A Stop-hook payload shaped like the real one (verified live, agy 1.0.5).

    ``fully_idle=None`` omits the field, mirroring agy: the proto bool is
    ``omitempty`` so a not-fully-idle Stop drops it entirely and only a
    fully-idle Stop emits ``"fullyIdle":true``.
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


def test_not_idle_omitted_field_keeps_the_marker(tmp_path: Path) -> None:
    """A not-fully-idle Stop omits fullyIdle (omitempty) -> agent stays RUNNING."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(fully_idle=None))
    assert _marker(tmp_path).exists()


def test_explicit_false_keeps_the_marker(tmp_path: Path) -> None:
    """An explicit ``"fullyIdle":false`` also keeps the marker (defensive)."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(fully_idle=False))
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
    """An unset MNGR_AGENT_STATE_DIR is a wiring error: fail loudly, not silently.

    mngr always sets it, and agy invokes this script via a path that embeds it,
    so reaching the body unset means misconfiguration -- we surface it (stderr,
    non-zero exit) rather than swallow it or remove a marker at the filesystem
    root. We still keep stdout empty so the Stop hook never emits a result.
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
