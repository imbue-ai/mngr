"""Tests for set_active_marker.sh, the PreInvocation marker/root hook.

The script touches the `active` marker and, when the marker was absent (a turn
boundary), records the payload's conversation id as the turn's root in
`root_conversation`. clear_active_marker_when_idle.sh then clears the marker
only for that root, so subagents (which share the hook) can't flip the agent to
WAITING. The tests pin: turn-opener records root + sets marker, a mid-turn
invocation does NOT overwrite the root (so subagents don't steal it), a missing
conversation id still sets the marker, stdout silence (agy treats PreInvocation
stdout as injected steps), and loud failure on a missing state dir.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT_PATH = Path(__file__).parent / "set_active_marker.sh"

_ROOT_CONV = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_SUB_CONV = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _payload(conv_id: str | None) -> str:
    """A PreInvocation payload shaped like the real one (verified live, agy 1.0.5)."""
    fields = []
    if conv_id is not None:
        fields.append(f'"conversationId":"{conv_id}"')
    fields.append('"invocationNum":0')
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


def _root_file(state_dir: Path) -> Path:
    return state_dir / "root_conversation"


def test_turn_opener_records_root_and_sets_marker(tmp_path: Path) -> None:
    result = _run(tmp_path, _payload(_ROOT_CONV))
    assert _marker(tmp_path).exists()
    assert _root_file(tmp_path).read_text() == _ROOT_CONV
    # PreInvocation handlers must stay silent: non-empty stdout = injected steps.
    assert result.stdout == ""


def test_mid_turn_invocation_does_not_overwrite_root(tmp_path: Path) -> None:
    """While the marker is present (turn in progress), a later invocation -- e.g.
    a subagent's -- must not steal the root, so the root agent keeps owning the
    turn's WAITING transition."""
    _run(tmp_path, _payload(_ROOT_CONV))
    _run(tmp_path, _payload(_SUB_CONV))
    assert _root_file(tmp_path).read_text() == _ROOT_CONV
    assert _marker(tmp_path).exists()


def test_new_turn_after_clear_records_new_root(tmp_path: Path) -> None:
    """Once the marker is cleared (turn done), the next opener re-roots -- this is
    what keeps /clear, /fork, /switch, and resume correct."""
    _run(tmp_path, _payload(_ROOT_CONV))
    # Simulate the clear hook ending the turn (marker removed).
    _marker(tmp_path).unlink()
    _run(tmp_path, _payload(_SUB_CONV))
    assert _root_file(tmp_path).read_text() == _SUB_CONV
    assert _marker(tmp_path).exists()


def test_missing_conversation_id_still_sets_marker(tmp_path: Path) -> None:
    """A payload without a conversation id still marks RUNNING; it just records no
    root (the clear hook's liveness fallback then applies)."""
    _run(tmp_path, _payload(None))
    assert _marker(tmp_path).exists()
    assert not _root_file(tmp_path).exists()


def test_garbage_stdin_still_sets_marker(tmp_path: Path) -> None:
    result = _run(tmp_path, "not json at all\n")
    assert _marker(tmp_path).exists()
    assert not _root_file(tmp_path).exists()
    assert result.stdout == ""


def test_missing_state_dir_fails_loudly(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH)],
        input=_payload(_ROOT_CONV),
        env={k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "MNGR_AGENT_STATE_DIR" in result.stderr
