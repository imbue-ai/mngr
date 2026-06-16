"""Tests for statusline.sh, the agy statusLine command that owns agent lifecycle.

agy invokes this on every agent-state change, piping a JSON payload on stdin
(`agent_state`, `conversation_id`, `model`, ...). The script maintains the
`active` marker BaseAgent reads for RUNNING/WAITING (active iff `agent_state` is
a busy state -- a denylist excluding idle/initializing/authenticating/empty),
records the root `conversation_id` for resume, and fires the tmux submission
signal when busy. mngr's statusLine is lifecycle-only and prints nothing of its
own (agy already shows working/idle), so stdout is empty unless a user statusLine
is composed, in which case the row is that command's output verbatim. The tests
pin: marker set when working / cleared on idle/initializing/authenticating,
root_conversation written from conversation_id and not clobbered by empty/garbage
payloads, an empty row with no user statusLine (the user's output verbatim when
one is composed), and loud failure on a missing state dir. (`tmux wait-for` is
`|| true`-guarded, so it is a no-op without a usable TMUX in tests.)
"""

from __future__ import annotations

import importlib.resources
import os
import subprocess
from pathlib import Path

from imbue.mngr import resources as mngr_resources

_SCRIPT_PATH = Path(__file__).parent / "statusline.sh"

_ROOT_CONV = "2005e9cc-93d7-4685-b96c-8da612ab8165"
_OTHER_CONV = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_MODEL = "Claude Sonnet 4.6 (Thinking)"


def _payload(*, agent_state: str | None, conversation_id: str | None = _ROOT_CONV, model: str | None = _MODEL) -> str:
    """A statusLine payload shaped like the real one (verified live, agy 1.0.6/1.0.7)."""
    fields = []
    if agent_state is not None:
        fields.append(f'"agent_state":"{agent_state}"')
    if conversation_id is not None:
        fields.append(f'"conversation_id":"{conversation_id}"')
    if model is not None:
        fields.append(f'"model":"{model}"')
    fields.append('"context_window":{"total_input_tokens":173}')
    return "{" + ",".join(fields) + "}"


def _run(state_dir: Path, payload: str) -> subprocess.CompletedProcess[str]:
    # Drop TMUX so the wait-for sub-shell can't reach a real tmux server during
    # tests; the `|| true` guard makes it a no-op regardless.
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    env["MNGR_AGENT_STATE_DIR"] = str(state_dir)
    return subprocess.run(
        ["bash", str(_SCRIPT_PATH)],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _marker(state_dir: Path) -> Path:
    return state_dir / "active"


def _root_file(state_dir: Path) -> Path:
    return state_dir / "root_conversation"


def test_working_sets_marker_and_records_root(tmp_path: Path) -> None:
    result = _run(tmp_path, _payload(agent_state="working"))
    assert _marker(tmp_path).exists()
    assert _root_file(tmp_path).read_text() == _ROOT_CONV
    # mngr is lifecycle-only and prints nothing of its own (agy shows working/idle
    # itself), so with no user statusLine the rendered row is empty.
    assert result.stdout == ""


def test_idle_clears_marker(tmp_path: Path) -> None:
    """`idle` is the canonical done state -> WAITING."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(agent_state="idle"))
    assert not _marker(tmp_path).exists()


def test_initializing_clears_marker(tmp_path: Path) -> None:
    """`initializing` is a not-yet-working state -> WAITING (denylist member)."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(agent_state="initializing"))
    assert not _marker(tmp_path).exists()


def test_authenticating_clears_marker(tmp_path: Path) -> None:
    """`authenticating` is a not-yet-working state -> WAITING (denylist member)."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(agent_state="authenticating"))
    assert not _marker(tmp_path).exists()


def test_unknown_busy_state_sets_marker(tmp_path: Path) -> None:
    """The denylist means any state outside {idle, initializing, authenticating, ""}
    counts as RUNNING -- a future busy state (e.g. "responding") keeps the marker."""
    _run(tmp_path, _payload(agent_state="responding"))
    assert _marker(tmp_path).exists()


def test_empty_agent_state_clears_marker(tmp_path: Path) -> None:
    """An empty/absent agent_state is treated as not-working (never falsely RUNNING)."""
    _marker(tmp_path).touch()
    _run(tmp_path, _payload(agent_state=None))
    assert not _marker(tmp_path).exists()


def test_empty_payload_does_not_clobber_recorded_root(tmp_path: Path) -> None:
    """A later payload without a conversation_id must not erase the recorded root."""
    _run(tmp_path, _payload(agent_state="working"))
    assert _root_file(tmp_path).read_text() == _ROOT_CONV
    _run(tmp_path, _payload(agent_state="idle", conversation_id=None))
    assert _root_file(tmp_path).read_text() == _ROOT_CONV


def test_root_conversation_updates_to_latest_root(tmp_path: Path) -> None:
    """agy always reports the root id here, so recording the latest keeps resume
    correct across /clear, /fork, /switch, and resume."""
    _run(tmp_path, _payload(agent_state="working", conversation_id=_ROOT_CONV))
    _run(tmp_path, _payload(agent_state="working", conversation_id=_OTHER_CONV))
    assert _root_file(tmp_path).read_text() == _OTHER_CONV


def test_garbage_stdin_records_no_root_and_clears_marker(tmp_path: Path) -> None:
    """Non-JSON stdin parses no state (-> not-working) and no id (-> no root)."""
    _marker(tmp_path).touch()
    result = _run(tmp_path, "not json at all\n")
    assert not _marker(tmp_path).exists()
    assert not _root_file(tmp_path).exists()
    # mngr prints nothing of its own, and no user statusLine here, so stdout is empty.
    assert result.stdout == ""


def test_missing_state_dir_fails_loudly(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH)],
        input=_payload(agent_state="working"),
        env={k: v for k, v in os.environ.items() if k != "MNGR_AGENT_STATE_DIR"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "MNGR_AGENT_STATE_DIR" in result.stderr


def _user_cmd_file(state_dir: Path) -> Path:
    return state_dir / "user_statusline_command"


def test_composes_user_statusline_output(tmp_path: Path) -> None:
    """When a user statusLine command is recorded, the row is exactly its output.

    mngr prints nothing of its own, so the rendered row is the user's command
    output verbatim (no glyph, no separator). The user command receives the same
    payload on stdin that agy delivers; here it just echoes a literal.
    """
    _user_cmd_file(tmp_path).write_text("echo USER-PART")
    result = _run(tmp_path, _payload(agent_state="working"))
    assert result.stdout.strip() == "USER-PART"


def test_user_statusline_receives_payload_on_stdin(tmp_path: Path) -> None:
    """The composed user command gets the agy payload on stdin (so it can render from it)."""
    # `cat` echoes whatever it received on stdin -> proves the payload was piped through.
    _user_cmd_file(tmp_path).write_text("cat")
    result = _run(tmp_path, _payload(agent_state="working"))
    assert _ROOT_CONV in result.stdout


def test_empty_user_statusline_file_renders_empty_row(tmp_path: Path) -> None:
    """An empty user_statusline_command file (no command recorded) renders nothing."""
    _user_cmd_file(tmp_path).write_text("")
    result = _run(tmp_path, _payload(agent_state="working"))
    assert result.stdout == ""


def test_failing_user_statusline_does_not_break_side_effects(tmp_path: Path) -> None:
    """A user command that exits non-zero / errors can't break mngr's side-effects.

    The marker (the lifecycle source of truth, read by BaseAgent) is still
    maintained, and the failing command emits no row.
    """
    _user_cmd_file(tmp_path).write_text("this-command-does-not-exist-xyz")
    result = _run(tmp_path, _payload(agent_state="working"))
    assert _marker(tmp_path).exists()
    assert result.stdout == ""


def _install_flush_stubs(state_dir: Path) -> Path:
    """Install the real shared common-transcript lib (so statusline.sh's source
    succeeds and mngr_common_transcript_flush is defined) plus stub stream/common
    transcript scripts that record that they ran (and whether the active marker
    was still present at that moment, to pin the flush-before-clear ordering).
    Returns the sentinel path the stubs append to."""
    commands = state_dir / "commands"
    commands.mkdir(parents=True, exist_ok=True)
    (commands / "mngr_common_transcript_lib.sh").write_text(
        importlib.resources.files(mngr_resources).joinpath("mngr_common_transcript_lib.sh").read_text()
    )
    sentinel = state_dir / "flush_sentinel"
    for name in ("stream_transcript.sh", "common_transcript.sh"):
        script = commands / name
        script.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "{name}:marker=$([ -e "$MNGR_AGENT_STATE_DIR/active" ] && echo present || echo gone)" '
            '>> "$MNGR_AGENT_STATE_DIR/flush_sentinel"\n'
        )
        script.chmod(0o755)
    return sentinel


def test_busy_to_idle_edge_flushes_before_clearing_marker(tmp_path: Path) -> None:
    """On the busy->idle edge the transcript converters are flushed (single-pass)
    BEFORE the active marker is cleared, so a WAITING-signal consumer that reads
    the common transcript can't outrun the converter."""
    sentinel = _install_flush_stubs(tmp_path)
    # A prior busy sample left the marker present; this idle is the busy->idle edge.
    _marker(tmp_path).touch()

    _run(tmp_path, _payload(agent_state="idle"))

    assert not _marker(tmp_path).exists()
    # Both converters ran (marker still present at flush time -> flush precedes
    # the clear), in pipeline order: raw streamer first, then common converter.
    assert sentinel.read_text().splitlines() == [
        "stream_transcript.sh:marker=present",
        "common_transcript.sh:marker=present",
    ]


def test_idle_without_prior_busy_does_not_flush(tmp_path: Path) -> None:
    """Idle/startup samples with no marker present (not a busy->idle edge) must
    not pay for a synchronous flush."""
    sentinel = _install_flush_stubs(tmp_path)
    _run(tmp_path, _payload(agent_state="idle"))
    assert not _marker(tmp_path).exists()
    assert not sentinel.exists()


def test_working_does_not_flush(tmp_path: Path) -> None:
    """Entering a busy state sets the marker and never flushes."""
    sentinel = _install_flush_stubs(tmp_path)
    _run(tmp_path, _payload(agent_state="working"))
    assert _marker(tmp_path).exists()
    assert not sentinel.exists()
