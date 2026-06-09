"""Tests for clear_active_marker.sh, the codex Stop hook.

The script removes the per-agent ``active`` marker only on the ROOT agent's
Stop: the payload's session id must match the root recorded in
``codex_root_session`` (written by set_active_marker.sh). codex's Stop is
already root-only (Task subagents fire a separate SubagentStop that mngr never
hooks), so -- unlike antigravity -- there is no fullyIdle flag to check; the only
thing guarded against is a *separate* nested codex process sharing this
CODEX_HOME whose Stop carries a different session id. The tests pin: root Stop
clears, a different session id keeps the marker, the no-root liveness fallback,
stdout silence (codex can treat Stop-hook stdout as a stop-blocking result),
garbage tolerance, and loud failure on a missing state dir.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SET_SCRIPT_PATH = Path(__file__).parent / "set_active_marker.sh"
_CLEAR_SCRIPT_PATH = Path(__file__).parent / "clear_active_marker.sh"

_ROOT_SESSION = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_NESTED_SESSION = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _payload(session_id: str | None) -> str:
    """A Stop-hook payload shaped like the real one (verified live)."""
    fields = []
    if session_id is not None:
        fields.append(f'"session_id":"{session_id}"')
    fields.append('"turn_id":"cccccccc-cccc-cccc-cccc-cccccccccccc"')
    fields.append('"hook_event_name":"Stop"')
    fields.append('"stop_hook_active":false')
    fields.append('"last_assistant_message":"all done"')
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


def _set_root(state_dir: Path, session_id: str) -> None:
    """Record ``session_id`` as the turn's root (as set_active_marker.sh would)."""
    (state_dir / "codex_root_session").write_text(session_id)


def test_root_stop_clears_the_marker(tmp_path: Path) -> None:
    _set_root(tmp_path, _ROOT_SESSION)
    _marker(tmp_path).touch()
    result = _run(tmp_path, _payload(_ROOT_SESSION))
    assert not _marker(tmp_path).exists()
    # Stop handlers must stay silent: stdout is a result that can block the stop.
    assert result.stdout == ""


def test_different_session_id_keeps_the_marker(tmp_path: Path) -> None:
    """A Stop from a nested codex (a different session id) must NOT flip the
    still-working root agent to WAITING."""
    _set_root(tmp_path, _ROOT_SESSION)
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(_NESTED_SESSION))
    assert _marker(tmp_path).exists()


def test_no_root_recorded_falls_back_to_clearing(tmp_path: Path) -> None:
    """If no root was recorded, a Stop still clears -- a liveness fallback so a
    failure to record the root can't strand the agent in RUNNING."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(_ROOT_SESSION))
    assert not _marker(tmp_path).exists()


def test_empty_root_file_falls_back_to_clearing(tmp_path: Path) -> None:
    """An empty root file is treated like an absent one (liveness fallback)."""
    _set_root(tmp_path, "")
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(_NESTED_SESSION))
    assert not _marker(tmp_path).exists()


def test_missing_marker_is_a_noop(tmp_path: Path) -> None:
    """Clearing an already-absent marker succeeds quietly (rm -f)."""
    _set_root(tmp_path, _ROOT_SESSION)
    result = _run(tmp_path, _payload(_ROOT_SESSION))
    assert not _marker(tmp_path).exists()
    assert result.returncode == 0


def test_garbage_stdin_with_root_recorded_keeps_the_marker(tmp_path: Path) -> None:
    """Non-JSON stdin yields no session id; with a root recorded the mismatch
    leaves the marker (never disrupts codex)."""
    _set_root(tmp_path, _ROOT_SESSION)
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
        input=_payload(_ROOT_SESSION),
        env={k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "MNGR_AGENT_STATE_DIR" in result.stderr


def _pre(state_dir: Path, session_id: str) -> None:
    """Drive set_active_marker.sh for session ``session_id`` (a UserPromptSubmit)."""
    subprocess.run(
        ["bash", str(_SET_SCRIPT_PATH)],
        input=f'{{"session_id":"{session_id}","hook_event_name":"UserPromptSubmit"}}',
        env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
        capture_output=True,
        text=True,
        check=True,
    )


def test_real_root_nested_interleaving_only_clears_on_root_stop(tmp_path: Path) -> None:
    """End-to-end replay through the real set/clear scripts.

    The root opens the turn; a nested codex sharing this CODEX_HOME opens a prompt
    (must not re-root while the marker is present) and then stops; the marker must
    survive the nested codex's Stop and clear only on the root's Stop.
    """
    marker = _marker(tmp_path)

    # Root opens the turn.
    _pre(tmp_path, _ROOT_SESSION)
    assert marker.exists()
    # A nested codex opens a prompt (must not re-root) and then stops.
    _pre(tmp_path, _NESTED_SESSION)
    _run(tmp_path, _payload(_NESTED_SESSION))
    assert marker.exists(), "a nested codex Stop must not clear the root's marker"
    # Root finishes.
    _run(tmp_path, _payload(_ROOT_SESSION))
    assert not marker.exists(), "root Stop must clear the marker"
