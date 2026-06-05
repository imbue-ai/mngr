"""Tests for clear_active_marker_when_idle.sh, the Stop hook idle gate.

The script removes the per-agent ``active`` marker only on the ROOT agent's
fully-idle Stop: the payload must report ``"fullyIdle":true`` AND its
conversation id must match the root recorded in ``root_conversation`` (written
by set_active_marker.sh). Subagents share the hook and fire their own
``fullyIdle:true`` Stop while the root still works, so those must NOT clear the
marker. The tests pin: root fully-idle clears, subagent fully-idle keeps,
interim ``"fullyIdle":false`` keeps, absent field keeps, the no-root liveness
fallback, whitespace tolerance, stdout silence (agy can treat Stop-hook stdout
as a stop-blocking result), garbage tolerance, loud failure on a missing state
dir, and a replay of the real root+subagent interleaving captured live.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SET_SCRIPT_PATH = Path(__file__).parent / "set_active_marker.sh"
_CLEAR_SCRIPT_PATH = Path(__file__).parent / "clear_active_marker_when_idle.sh"

_ROOT_CONV = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_SUB_CONV = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _payload(*, fully_idle: bool | None, conv_id: str = _ROOT_CONV) -> str:
    """A Stop-hook payload shaped like the real one (verified live, agy 1.0.5).

    agy emits ``fullyIdle`` explicitly (interim Stop ``false``, final ``true``).
    ``fully_idle=None`` drops the field to exercise the defensive path.
    """
    brain = f"/home/u/.gemini/antigravity-cli/brain/{conv_id}"
    fields = [
        f'"artifactDirectoryPath":"{brain}"',
        f'"conversationId":"{conv_id}"',
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
        ["bash", str(_CLEAR_SCRIPT_PATH)],
        input=payload,
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        check=True,
    )


def _marker(state_dir: Path) -> Path:
    return state_dir / "active"


def _set_root(state_dir: Path, conv_id: str) -> None:
    """Record ``conv_id`` as the turn's root (as set_active_marker.sh would)."""
    (state_dir / "root_conversation").write_text(conv_id)


def test_root_fully_idle_clears_the_marker(tmp_path: Path) -> None:
    _set_root(tmp_path, _ROOT_CONV)
    _marker(tmp_path).touch()
    result = _run(tmp_path, _payload(fully_idle=True, conv_id=_ROOT_CONV))
    assert not _marker(tmp_path).exists()
    # Stop handlers must stay silent: stdout is a structured result that can
    # block the stop.
    assert result.stdout == ""


def test_subagent_fully_idle_keeps_the_marker(tmp_path: Path) -> None:
    """A subagent finishing (its own fullyIdle:true, a different conversation) must
    NOT flip the still-working root agent to WAITING.

    This is the case verified live against agy 1.0.5: a subagent's fullyIdle:true
    Stop arrives while the root's next Stop is still fullyIdle:false.
    """
    _set_root(tmp_path, _ROOT_CONV)
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(fully_idle=True, conv_id=_SUB_CONV))
    assert _marker(tmp_path).exists()


def test_interim_false_keeps_the_marker(tmp_path: Path) -> None:
    """An interim Stop sends ``"fullyIdle":false`` -> agent stays RUNNING."""
    _set_root(tmp_path, _ROOT_CONV)
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(fully_idle=False, conv_id=_ROOT_CONV))
    assert _marker(tmp_path).exists()


def test_absent_field_keeps_the_marker(tmp_path: Path) -> None:
    """A payload missing fullyIdle keeps the marker (defensive, never WAITING-early)."""
    _set_root(tmp_path, _ROOT_CONV)
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(fully_idle=None, conv_id=_ROOT_CONV))
    assert _marker(tmp_path).exists()


def test_no_root_recorded_falls_back_to_clearing(tmp_path: Path) -> None:
    """If no root was recorded, a fullyIdle:true Stop still clears -- a liveness
    fallback so a failure to record the root can't strand the agent in RUNNING."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(fully_idle=True, conv_id=_ROOT_CONV))
    assert not _marker(tmp_path).exists()


def test_pretty_printed_whitespace_still_matches(tmp_path: Path) -> None:
    """Whitespace between key, colon, and value does not defeat the match."""
    # No root recorded -> the liveness fallback clears on fullyIdle:true.
    _marker(tmp_path).touch()
    _run(tmp_path, '{\n  "fullyIdle": true\n}')
    assert not _marker(tmp_path).exists()


def test_missing_marker_on_fully_idle_is_a_noop(tmp_path: Path) -> None:
    """Clearing an already-absent marker succeeds quietly (rm -f)."""
    _set_root(tmp_path, _ROOT_CONV)
    result = _run(tmp_path, _payload(fully_idle=True, conv_id=_ROOT_CONV))
    assert not _marker(tmp_path).exists()
    assert result.returncode == 0


def test_garbage_stdin_keeps_the_marker(tmp_path: Path) -> None:
    """Non-JSON stdin matches nothing, so the marker is preserved (never disrupts agy)."""
    _set_root(tmp_path, _ROOT_CONV)
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
        ["bash", str(_CLEAR_SCRIPT_PATH)],
        input=_payload(fully_idle=True),
        env={k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "MNGR_AGENT_STATE_DIR" in result.stderr


def _pre(state_dir: Path, conv_id: str) -> None:
    """Drive set_active_marker.sh for conversation ``conv_id`` (a PreInvocation)."""
    subprocess.run(
        ["bash", str(_SET_SCRIPT_PATH)],
        input=f'{{"conversationId":"{conv_id}","invocationNum":0}}',
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        check=True,
    )


def test_real_root_subagent_interleaving_only_clears_on_root_done(tmp_path: Path) -> None:
    """End-to-end replay of the root+subagent event order captured live (agy 1.0.5).

    Drives the actual set/clear scripts through the real interleaving: the root
    opens the turn, spawns a subagent, the subagent finishes (fullyIdle:true)
    while the root is still working, then the root finishes. The marker must stay
    present through the subagent's completion and clear only on the root's.
    """
    marker = _marker(tmp_path)

    # Root opens the turn.
    _pre(tmp_path, _ROOT_CONV)
    assert marker.exists()
    # Root is now waiting on the subagent.
    _run(tmp_path, _payload(fully_idle=False, conv_id=_ROOT_CONV))
    assert marker.exists()
    # Subagent starts (must not re-root) and reports an interim Stop.
    _pre(tmp_path, _SUB_CONV)
    _run(tmp_path, _payload(fully_idle=False, conv_id=_SUB_CONV))
    assert marker.exists()
    # Subagent finishes (its own fullyIdle:true) -- the root is still working.
    _run(tmp_path, _payload(fully_idle=True, conv_id=_SUB_CONV))
    assert marker.exists(), "subagent completion must not clear the root's marker"
    # Root resumes, then finishes.
    _pre(tmp_path, _ROOT_CONV)
    _run(tmp_path, _payload(fully_idle=False, conv_id=_ROOT_CONV))
    assert marker.exists()
    _run(tmp_path, _payload(fully_idle=True, conv_id=_ROOT_CONV))
    assert not marker.exists(), "root completion must clear the marker"
