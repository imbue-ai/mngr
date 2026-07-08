"""Tests for set_active_marker.sh, the codex UserPromptSubmit marker/root hook.

The script sets the ``codex_root_active`` flag and recomputes the ``active``
marker under a lock and, when the marker was absent (a fresh root turn), records
the payload's session id as the turn's root in ``codex_root_session`` and the
rollout ``transcript_path`` in ``codex_transcript_path``. clear_active_marker.sh
then clears the root-turn flag only for that root session, so a nested/recursive
codex process sharing this CODEX_HOME can't flip the agent to WAITING. The tests
pin: turn-opener records root + transcript path + sets the marker via the
flag/recompute, a mid-turn invocation does NOT overwrite the root or transcript
path (so a nested codex doesn't steal them), a fresh root turn clears a stranded
permissions_waiting marker while a mid-turn (nested) invocation leaves it intact,
a missing session id still sets the marker, transcript paths with spaces/slashes
survive, stdout silence (codex treats UserPromptSubmit stdout as injected model
context), and loud failure on a missing state dir.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from imbue.mngr_codex.codex_config import SUBMIT_WAIT_CHANNEL_PREFIX
from imbue.mngr_codex.resources.testing import provision_commands_dir
from imbue.mngr_codex.resources.testing import run_codex_hook

_SCRIPT = "set_active_marker.sh"

_ROOT_SESSION = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_NESTED_SESSION = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_ROOT_TRANSCRIPT = "/home/u/.codex/sessions/2026/06/09/rollout-2026-06-09-aaaaaaaa.jsonl"
_NESTED_TRANSCRIPT = "/home/u/.codex/sessions/2026/06/09/rollout-2026-06-09-bbbbbbbb.jsonl"


def _payload(session_id: str | None, transcript_path: str | None) -> str:
    """A UserPromptSubmit payload shaped like the real one (verified live)."""
    fields = []
    if session_id is not None:
        fields.append(f'"session_id":"{session_id}"')
    fields.append('"turn_id":"cccccccc-cccc-cccc-cccc-cccccccccccc"')
    if transcript_path is not None:
        fields.append(f'"transcript_path":"{transcript_path}"')
    fields.append('"cwd":"/tmp/ws"')
    fields.append('"hook_event_name":"UserPromptSubmit"')
    fields.append('"prompt":"do the thing"')
    return "{" + ",".join(fields) + "}"


def _run(state_dir: Path, payload: str) -> subprocess.CompletedProcess[str]:
    return run_codex_hook(state_dir, _SCRIPT, payload)


def _marker(state_dir: Path) -> Path:
    return state_dir / "active"


def _root_active(state_dir: Path) -> Path:
    return state_dir / "codex_root_active"


def _root_file(state_dir: Path) -> Path:
    return state_dir / "codex_root_session"


def _transcript_file(state_dir: Path) -> Path:
    return state_dir / "codex_transcript_path"


def _permissions_waiting(state_dir: Path) -> Path:
    return state_dir / "permissions_waiting"


def test_turn_opener_records_root_transcript_and_sets_marker(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, (_SCRIPT,))
    result = _run(tmp_path, _payload(_ROOT_SESSION, _ROOT_TRANSCRIPT))
    assert _marker(tmp_path).exists()
    assert _root_active(tmp_path).exists()
    assert _root_file(tmp_path).read_text() == _ROOT_SESSION
    assert _transcript_file(tmp_path).read_text() == _ROOT_TRANSCRIPT
    # UserPromptSubmit handlers must stay silent: stdout = injected model context.
    assert result.stdout == ""


def test_mid_turn_invocation_does_not_overwrite_root_or_transcript(tmp_path: Path) -> None:
    """While the marker is present (turn in progress), a later invocation -- e.g.
    a nested codex sharing this CODEX_HOME -- must not steal the root or the
    transcript path, so the root agent keeps owning the turn's WAITING transition
    and the streamer keeps tailing the root's rollout."""
    provision_commands_dir(tmp_path, (_SCRIPT,))
    _run(tmp_path, _payload(_ROOT_SESSION, _ROOT_TRANSCRIPT))
    _run(tmp_path, _payload(_NESTED_SESSION, _NESTED_TRANSCRIPT))
    assert _root_file(tmp_path).read_text() == _ROOT_SESSION
    assert _transcript_file(tmp_path).read_text() == _ROOT_TRANSCRIPT
    assert _marker(tmp_path).exists()


def test_new_turn_after_clear_records_new_root(tmp_path: Path) -> None:
    """Once the marker is cleared (turn done), the next opener re-roots -- this is
    what keeps resume correct when codex opens a fresh rollout."""
    provision_commands_dir(tmp_path, (_SCRIPT,))
    _run(tmp_path, _payload(_ROOT_SESSION, _ROOT_TRANSCRIPT))
    # Simulate the clear hook ending the turn (marker + root-turn flag removed).
    _marker(tmp_path).unlink()
    _root_active(tmp_path).unlink()
    _run(tmp_path, _payload(_NESTED_SESSION, _NESTED_TRANSCRIPT))
    assert _root_file(tmp_path).read_text() == _NESTED_SESSION
    assert _transcript_file(tmp_path).read_text() == _NESTED_TRANSCRIPT
    assert _marker(tmp_path).exists()


def test_fresh_root_turn_clears_stranded_permissions_marker(tmp_path: Path) -> None:
    """A new root turn (marker absent) clears any leftover permissions_waiting so the
    turn does not inherit a prior denied/cancelled dialog's state once active is set.
    Second safety net alongside clear_active_marker.sh's Stop-time removal."""
    provision_commands_dir(tmp_path, (_SCRIPT,))
    _permissions_waiting(tmp_path).touch()
    _run(tmp_path, _payload(_ROOT_SESSION, _ROOT_TRANSCRIPT))
    assert _marker(tmp_path).exists()
    assert not _permissions_waiting(tmp_path).exists()


def test_mid_turn_invocation_does_not_clear_permissions_marker(tmp_path: Path) -> None:
    """While the marker is present (turn in progress), a later invocation -- e.g. a
    nested codex sharing this CODEX_HOME -- must not clear the root's
    permissions_waiting, so it can't erase a dialog the root is genuinely blocked on.
    Mirrors the root-session capture guard."""
    provision_commands_dir(tmp_path, (_SCRIPT,))
    _run(tmp_path, _payload(_ROOT_SESSION, _ROOT_TRANSCRIPT))
    # Root is now mid-turn and blocks on an approval dialog (PermissionRequest hook).
    _permissions_waiting(tmp_path).touch()
    _run(tmp_path, _payload(_NESTED_SESSION, _NESTED_TRANSCRIPT))
    assert _permissions_waiting(tmp_path).exists()


def test_transcript_path_with_spaces_and_slashes_is_captured(tmp_path: Path) -> None:
    """The rollout path is an arbitrary absolute path; spaces and slashes survive."""
    provision_commands_dir(tmp_path, (_SCRIPT,))
    spaced = "/home/My User/.codex/sessions/2026/06/09/rollout abc.jsonl"
    _run(tmp_path, _payload(_ROOT_SESSION, spaced))
    assert _transcript_file(tmp_path).read_text() == spaced


def test_missing_session_id_still_sets_marker(tmp_path: Path) -> None:
    """A payload without a session id still marks RUNNING; it just records no root
    (the clear hook's liveness fallback then applies). The transcript path is
    still captured independently."""
    provision_commands_dir(tmp_path, (_SCRIPT,))
    _run(tmp_path, _payload(None, _ROOT_TRANSCRIPT))
    assert _marker(tmp_path).exists()
    assert not _root_file(tmp_path).exists()
    assert _transcript_file(tmp_path).read_text() == _ROOT_TRANSCRIPT


def test_missing_transcript_path_still_records_root(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, (_SCRIPT,))
    _run(tmp_path, _payload(_ROOT_SESSION, None))
    assert _marker(tmp_path).exists()
    assert _root_file(tmp_path).read_text() == _ROOT_SESSION
    assert not _transcript_file(tmp_path).exists()


def test_garbage_stdin_still_sets_marker(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, (_SCRIPT,))
    result = _run(tmp_path, "not json at all\n")
    assert _marker(tmp_path).exists()
    assert not _root_file(tmp_path).exists()
    assert not _transcript_file(tmp_path).exists()
    assert result.stdout == ""


def test_signals_submit_channel_after_setting_the_marker() -> None:
    """The hook fires the `mngr-submit-<session>` wait-for channel AFTER the marker
    recompute, so a `send_message` waiter wakes only once the agent reads RUNNING.
    The prefix is pinned to codex_config's SUBMIT_WAIT_CHANNEL_PREFIX, which
    CodexAgent._send_enter_and_validate waits on -- this keeps the two literals in
    sync (the shell script can't import the constant)."""
    script = (Path(__file__).parent / _SCRIPT).read_text()
    signal = f'tmux wait-for -S "{SUBMIT_WAIT_CHANNEL_PREFIX}'
    assert signal in script
    assert script.index("codex_marker_recompute") < script.index(signal)


def test_submit_signal_is_skipped_headless_without_breaking_the_marker(tmp_path: Path) -> None:
    """In a headless run ($TMUX unset, as run_codex_hook arranges) the submit signal
    is skipped entirely, so the hook still sets the marker and stays silent -- and
    never invokes tmux (which would trip the resource guard)."""
    provision_commands_dir(tmp_path, (_SCRIPT,))
    result = _run(tmp_path, _payload(_ROOT_SESSION, _ROOT_TRANSCRIPT))
    assert _marker(tmp_path).exists()
    assert result.stdout == ""


def test_missing_state_dir_fails_loudly(tmp_path: Path) -> None:
    provision_commands_dir(tmp_path, (_SCRIPT,))
    result = subprocess.run(
        ["bash", str(tmp_path / "commands" / _SCRIPT)],
        input=_payload(_ROOT_SESSION, _ROOT_TRANSCRIPT),
        env={k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "MNGR_AGENT_STATE_DIR" in result.stderr
