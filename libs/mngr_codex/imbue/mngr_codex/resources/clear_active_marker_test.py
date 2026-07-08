"""Tests for clear_active_marker.sh, the codex Stop hook.

The script clears the ``codex_root_active`` flag and recomputes the ``active``
marker only on the ROOT agent's Stop: the payload's session id must match the
root recorded in ``codex_root_session`` (written by set_active_marker.sh).
Because codex subagents run asynchronously, the root's Stop fires while
subagents may still be running, so clearing the flag does NOT necessarily clear
the marker -- the recompute keeps it present while any per-subagent file under
``codex_subagents/`` remains. The tests pin: root Stop clears when no subagents,
a different session id (nested codex) leaves everything untouched, the no-root
liveness fallback, the async case (subagent in flight keeps the marker through
the root Stop), the reverse order, two-subagent gating, a concurrency smoke
test, the lock stale-break, the permissions_waiting safety-net clear (and that a
nested session leaves it alone), stdout silence, and loud failure on a missing
state dir.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from imbue.mngr_codex.resources.testing import install_common_transcript_flush_stub
from imbue.mngr_codex.resources.testing import provision_commands_dir
from imbue.mngr_codex.resources.testing import run_codex_hook

_SET_SCRIPT = "set_active_marker.sh"
_CLEAR_SCRIPT = "clear_active_marker.sh"
_START_SCRIPT = "subagent_started.sh"
_STOP_SCRIPT = "subagent_stopped.sh"
_ALL_SCRIPTS = (_SET_SCRIPT, _CLEAR_SCRIPT, _START_SCRIPT, _STOP_SCRIPT)

_ROOT_SESSION = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_NESTED_SESSION = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_AGENT_A = "11111111-1111-1111-1111-111111111111"
_AGENT_B = "22222222-2222-2222-2222-222222222222"


def _stop_payload(session_id: str | None) -> str:
    """A Stop-hook payload shaped like the real one (verified live)."""
    fields = []
    if session_id is not None:
        fields.append(f'"session_id":"{session_id}"')
    fields.append('"turn_id":"cccccccc-cccc-cccc-cccc-cccccccccccc"')
    fields.append('"hook_event_name":"Stop"')
    fields.append('"stop_hook_active":false')
    fields.append('"last_assistant_message":"all done"')
    return "{" + ",".join(fields) + "}"


def _prompt_payload(session_id: str) -> str:
    return f'{{"session_id":"{session_id}","hook_event_name":"UserPromptSubmit"}}'


def _subagent_payload(agent_id: str, event_name: str) -> str:
    return (
        f'{{"session_id":"{_ROOT_SESSION}","agent_id":"{agent_id}",'
        f'"agent_type":"general","hook_event_name":"{event_name}"}}'
    )


def _clear(state_dir: Path, session_id: str | None) -> subprocess.CompletedProcess[str]:
    return run_codex_hook(state_dir, _CLEAR_SCRIPT, _stop_payload(session_id))


def _prompt(state_dir: Path, session_id: str) -> None:
    run_codex_hook(state_dir, _SET_SCRIPT, _prompt_payload(session_id))


def _subagent_start(state_dir: Path, agent_id: str) -> None:
    run_codex_hook(state_dir, _START_SCRIPT, _subagent_payload(agent_id, "SubagentStart"))


def _subagent_stop(state_dir: Path, agent_id: str) -> None:
    run_codex_hook(state_dir, _STOP_SCRIPT, _subagent_payload(agent_id, "SubagentStop"))


def _marker(state_dir: Path) -> Path:
    return state_dir / "active"


def _root_active(state_dir: Path) -> Path:
    return state_dir / "codex_root_active"


def _permissions_waiting(state_dir: Path) -> Path:
    return state_dir / "permissions_waiting"


def _set_root(state_dir: Path, session_id: str) -> None:
    """Record ``session_id`` as the turn's root (as set_active_marker.sh would)."""
    (state_dir / "codex_root_session").write_text(session_id)


def test_root_stop_clears_the_marker(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _set_root(tmp_path, _ROOT_SESSION)
    _root_active(tmp_path).touch()
    _marker(tmp_path).touch()
    result = _clear(tmp_path, _ROOT_SESSION)
    assert not _marker(tmp_path).exists()
    assert not _root_active(tmp_path).exists()
    # Stop handlers must stay silent: stdout is a result that can block the stop.
    assert result.stdout == ""


def test_different_session_id_leaves_everything_untouched(tmp_path: Path) -> None:
    """A Stop from a nested codex (a different session id) must NOT flip the
    still-working root agent to WAITING, nor clear the root-turn flag."""
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _set_root(tmp_path, _ROOT_SESSION)
    _root_active(tmp_path).touch()
    _marker(tmp_path).touch()
    _clear(tmp_path, _NESTED_SESSION)
    assert _marker(tmp_path).exists()
    assert _root_active(tmp_path).exists()


def test_no_root_recorded_falls_back_to_clearing(tmp_path: Path) -> None:
    """If no root was recorded, a Stop still clears -- a liveness fallback so a
    failure to record the root can't strand the agent in RUNNING."""
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _root_active(tmp_path).touch()
    _marker(tmp_path).touch()
    _clear(tmp_path, _ROOT_SESSION)
    assert not _marker(tmp_path).exists()


def test_empty_root_file_falls_back_to_clearing(tmp_path: Path) -> None:
    """An empty root file is treated like an absent one (liveness fallback)."""
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _set_root(tmp_path, "")
    _root_active(tmp_path).touch()
    _marker(tmp_path).touch()
    _clear(tmp_path, _NESTED_SESSION)
    assert not _marker(tmp_path).exists()


def test_garbage_stdin_with_root_recorded_keeps_the_marker(tmp_path: Path) -> None:
    """Non-JSON stdin yields no session id; with a root recorded the mismatch
    leaves the marker (never disrupts codex)."""
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _set_root(tmp_path, _ROOT_SESSION)
    _root_active(tmp_path).touch()
    _marker(tmp_path).touch()
    result = run_codex_hook(tmp_path, _CLEAR_SCRIPT, "not json at all\n")
    assert _marker(tmp_path).exists()
    assert result.stdout == ""


def test_missing_state_dir_fails_loudly(tmp_path: Path) -> None:
    """An unset MNGR_AGENT_STATE_DIR is a wiring error: fail loudly (stderr,
    non-zero exit), never silently remove a marker at the filesystem root. Keep
    stdout empty so the Stop hook still emits no result.
    """
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    result = subprocess.run(
        ["bash", str(tmp_path / "commands" / _CLEAR_SCRIPT)],
        input=_stop_payload(_ROOT_SESSION),
        env={k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "MNGR_AGENT_STATE_DIR" in result.stderr


def test_async_subagent_keeps_marker_through_root_stop(tmp_path: Path) -> None:
    """The key async case: the root opens a turn, spawns a subagent, then the
    root's model loop ends (Stop) WHILE the subagent is still running. The marker
    must stay present until the subagent's own SubagentStop arrives."""
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _prompt(tmp_path, _ROOT_SESSION)
    assert _marker(tmp_path).exists()
    _subagent_start(tmp_path, _AGENT_A)
    # Root finishes first; the subagent is still in flight.
    _clear(tmp_path, _ROOT_SESSION)
    assert _marker(tmp_path).exists(), "root Stop must not clear the marker while a subagent runs"
    assert not _root_active(tmp_path).exists()
    # Subagent finishes -> now the marker clears.
    _subagent_stop(tmp_path, _AGENT_A)
    assert not _marker(tmp_path).exists()


def test_reverse_order_subagent_stop_before_root_stop(tmp_path: Path) -> None:
    """If the SubagentStop arrives before the root's Stop, the marker stays until
    the root Stop (the root turn is still active) and clears then."""
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _prompt(tmp_path, _ROOT_SESSION)
    _subagent_start(tmp_path, _AGENT_A)
    _subagent_stop(tmp_path, _AGENT_A)
    assert _marker(tmp_path).exists(), "root turn still active -> marker stays"
    _clear(tmp_path, _ROOT_SESSION)
    assert not _marker(tmp_path).exists()


def test_two_subagents_marker_stays_until_both_stop(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _prompt(tmp_path, _ROOT_SESSION)
    _subagent_start(tmp_path, _AGENT_A)
    _subagent_start(tmp_path, _AGENT_B)
    _clear(tmp_path, _ROOT_SESSION)
    assert _marker(tmp_path).exists()
    _subagent_stop(tmp_path, _AGENT_A)
    assert _marker(tmp_path).exists(), "one subagent still in flight"
    _subagent_stop(tmp_path, _AGENT_B)
    assert not _marker(tmp_path).exists()


# This smoke test forks ~32 short-lived bash subprocesses (4 per iteration). That
# work is well under a second when the machine is idle, but on a heavily loaded CI
# runner the fork/exec alone can blow past the suite-wide 10s timeout, so give it
# generous headroom (a real deadlock would still hang far longer) and let offload
# retry it as a known-flaky test.
@pytest.mark.flaky
@pytest.mark.timeout(60)
def test_concurrent_root_stop_and_last_subagent_stop_clears_marker(tmp_path: Path) -> None:
    """Smoke test the lock: race the root Stop against the last SubagentStop a
    handful of times; the marker must always end CLEARED, never stranded."""
    # Each iteration uses a fresh state dir so there is no cross-iteration state.
    for iteration_idx in range(8):
        state_dir = tmp_path / f"iter_{iteration_idx}"
        state_dir.mkdir()
        provision_commands_dir(state_dir, _ALL_SCRIPTS)
        _prompt(state_dir, _ROOT_SESSION)
        _subagent_start(state_dir, _AGENT_A)
        # Launch both terminal events concurrently.
        clear_proc = subprocess.Popen(
            ["bash", str(state_dir / "commands" / _CLEAR_SCRIPT)],
            stdin=subprocess.PIPE,
            env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
            text=True,
        )
        stop_proc = subprocess.Popen(
            ["bash", str(state_dir / "commands" / _STOP_SCRIPT)],
            stdin=subprocess.PIPE,
            env={**os.environ, "MNGR_AGENT_STATE_DIR": str(state_dir)},
            text=True,
        )
        clear_proc.communicate(_stop_payload(_ROOT_SESSION))
        stop_proc.communicate(_subagent_payload(_AGENT_A, "SubagentStop"))
        assert clear_proc.wait() == 0
        assert stop_proc.wait() == 0
        assert not _marker(state_dir).exists(), "concurrent terminal events must not strand the marker"


def test_root_stop_clears_stranded_permissions_waiting(tmp_path: Path) -> None:
    """Safety net: a permissions_waiting marker left behind by a cancelled/denied
    dialog (PostToolUse never ran) is cleared on the root's Stop, so the agent
    can't report PERMISSIONS-WAITING forever."""
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _set_root(tmp_path, _ROOT_SESSION)
    _root_active(tmp_path).touch()
    _marker(tmp_path).touch()
    _permissions_waiting(tmp_path).touch()
    _clear(tmp_path, _ROOT_SESSION)
    assert not _permissions_waiting(tmp_path).exists()


def test_nested_session_stop_leaves_permissions_waiting(tmp_path: Path) -> None:
    """A nested codex's Stop (a different session id) returns before the clear, so
    it must not remove the root's permissions_waiting marker."""
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _set_root(tmp_path, _ROOT_SESSION)
    _root_active(tmp_path).touch()
    _permissions_waiting(tmp_path).touch()
    _clear(tmp_path, _NESTED_SESSION)
    assert _permissions_waiting(tmp_path).exists()


def test_root_stop_flushes_common_transcript_when_waiting(tmp_path: Path) -> None:
    """Once the root Stop leaves the agent WAITING (no subagents in flight), the
    hook runs the turn-end common-transcript flush so a consumer reading the
    final message on the WAITING signal doesn't race the 5s converter daemon."""
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _set_root(tmp_path, _ROOT_SESSION)
    _root_active(tmp_path).touch()
    _marker(tmp_path).touch()
    sentinel = tmp_path / "flush_ran"
    install_common_transcript_flush_stub(tmp_path, sentinel)

    _clear(tmp_path, _ROOT_SESSION)

    assert not _marker(tmp_path).exists()
    assert sentinel.exists(), "turn-end flush must run once the agent goes WAITING"


def test_root_stop_skips_flush_while_subagent_in_flight(tmp_path: Path) -> None:
    """The flush fires only on the WAITING transition: if a subagent is still in
    flight after the root Stop (the marker stays), the flush must not run."""
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _prompt(tmp_path, _ROOT_SESSION)
    _subagent_start(tmp_path, _AGENT_A)
    sentinel = tmp_path / "flush_ran"
    install_common_transcript_flush_stub(tmp_path, sentinel)

    _clear(tmp_path, _ROOT_SESSION)

    assert _marker(tmp_path).exists()
    assert not sentinel.exists(), "flush must not run while the agent is still RUNNING"


def test_lock_stale_break_lets_hook_proceed(tmp_path: Path) -> None:
    """A lock dir orphaned by a crashed hook (older than one minute) is stolen so
    the next hook still proceeds rather than hanging forever."""
    provision_commands_dir(tmp_path, _ALL_SCRIPTS)
    _set_root(tmp_path, _ROOT_SESSION)
    _root_active(tmp_path).touch()
    _marker(tmp_path).touch()
    # Pre-create the lock dir and backdate it well past the one-minute stale cap.
    lock_dir = tmp_path / "codex_marker.lock"
    lock_dir.mkdir()
    old_time = lock_dir.stat().st_mtime - 600
    os.utime(lock_dir, (old_time, old_time))
    _clear(tmp_path, _ROOT_SESSION)
    assert not _marker(tmp_path).exists(), "a stale lock must be stolen so the hook proceeds"
