"""Regression test: claude_background_tasks.sh must exit promptly when its target
session is gone, even if a prefix-colliding sibling session is still alive.

The script polls ``tmux has-session -t "=$SESSION_NAME"`` in its main loop. The
leading ``=`` forces tmux exact-session matching. Without it, tmux's default
session-name prefix matching would route ``has-session`` to a still-alive sibling
session whose name starts with ``$SESSION_NAME``, so the loop would never exit
when our agent's session has actually been torn down. That leaks the transcript
streamer and the common-transcript converter children for the gone agent.

This test sets up the exact prefix-collision scenario, runs the script against
the *stopped* (shorter-name) session, and asserts the script exits promptly. If
the ``=`` is dropped, the script will loop forever (its inner sleep is 15s) and
the subprocess.run timeout will fail this test loudly.

A near-identical test exists for gemini_background_tasks.sh in
``libs/mngr_gemini/imbue/mngr_gemini/test_background_tasks_prefix_collision.py``.
"""

import importlib.resources
import os
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.mngr import resources as _mngr_resources
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_claude import resources as _claude_resources


@pytest.fixture
def colliding_session_pair() -> Generator[tuple[str, str], None, None]:
    """Create two tmux sessions whose names share a prefix, then kill the shorter.

    Yields ``(stopped_name, alive_name)`` where ``stopped_name`` is a prefix of
    ``alive_name``. The alive session is cleaned up after the test.
    """
    stopped_name = f"mngr-bgtask-pfx-{get_short_random_string()}"
    alive_name = f"{stopped_name}-sibling"
    subprocess.run(["tmux", "new-session", "-d", "-s", stopped_name, "sleep", "60"], check=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", alive_name, "sleep", "60"], check=True)
    # Pass the exact-match `=` form directly: in subprocess-argv mode there is
    # no shell to interpret quoting, so the TmuxSessionTarget.as_shell_arg()
    # helper (which shlex-quotes for shell embedding) is the wrong tool here.
    subprocess.run(["tmux", "kill-session", "-t", f"={stopped_name}"], check=True)
    try:
        yield (stopped_name, alive_name)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", f"={alive_name}"], check=False)


@pytest.fixture
def state_dir_with_log_lib(tmp_path: Path) -> Path:
    """Lay out a minimal MNGR_AGENT_STATE_DIR the background script needs.

    The script sources ``$MNGR_AGENT_STATE_DIR/commands/mngr_log.sh`` and
    writes to ``$MNGR_AGENT_STATE_DIR/{activity,events,logs}/...``. We copy in
    the real mngr_log.sh and pre-create the directories it expects.
    """
    state_dir = tmp_path / "state"
    (state_dir / "commands").mkdir(parents=True)
    (state_dir / "activity").mkdir()
    (state_dir / "events" / "logs" / "claude_background_tasks").mkdir(parents=True)
    (state_dir / "logs").mkdir()

    log_lib = importlib.resources.files(_mngr_resources) / "mngr_log.sh"
    with importlib.resources.as_file(log_lib) as src:
        shutil.copy(src, state_dir / "commands" / "mngr_log.sh")
    return state_dir


@pytest.mark.tmux
def test_claude_background_tasks_exits_when_target_session_is_gone_despite_prefix_collision(
    colliding_session_pair: tuple[str, str],
    state_dir_with_log_lib: Path,
) -> None:
    """The script must exit promptly when its named session is gone, even if a
    prefix-colliding sibling is still alive. With the ``=`` exact-match prefix in
    the polling loop, the first ``has-session`` check fails and the loop never
    enters. Without it, ``has-session`` would prefix-match the sibling and the
    loop would never terminate.
    """
    stopped, _alive = colliding_session_pair

    # TMUX_TMPDIR comes from mngr's per-test isolated tmux server fixture
    # (autouse). We pass only what the script needs rather than the full env.
    env = {
        "PATH": os.environ["PATH"],
        "TMUX_TMPDIR": os.environ["TMUX_TMPDIR"],
        "MNGR_AGENT_STATE_DIR": str(state_dir_with_log_lib),
    }

    # With the fix the script exits in <1s. Without it, the script enters the
    # main loop (whose inner sleep is 15s) and never exits -- subprocess.run
    # surfaces this as TimeoutExpired and the test fails.
    script = importlib.resources.files(_claude_resources) / "claude_background_tasks.sh"
    with importlib.resources.as_file(script) as script_path:
        result = subprocess.run(
            ["bash", str(script_path), stopped],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )

    # The script's inner sleep is 15s. If the bug is back, the first iteration
    # finds the sibling (prefix match) and enters the loop, so the 5s
    # subprocess.run timeout fires inside the first sleep and raises
    # TimeoutExpired, failing this test. With the fix, the loop never enters
    # and the script exits well within the timeout.
    assert result.returncode == 0, (
        f"Background script exited non-zero: returncode={result.returncode}\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    # returncode == 0 alone cannot distinguish "exited because the target
    # session is gone (correct)" from "exited 0 for an unrelated early reason"
    # (e.g. a pidfile-collision early-exit at the top of the script). The
    # final ``log_info "... (session ended)"`` is only reached after the
    # polling loop's exact-match ``has-session`` check fails and the loop body
    # never runs, so its presence proves the script took the gone-session path
    # rather than short-circuiting earlier. ``log_info`` writes JSONL to the
    # events log (not stdout), so we assert against that file.
    events_log = state_dir_with_log_lib / "events" / "logs" / "claude_background_tasks" / "events.jsonl"
    assert events_log.exists(), f"Expected background-tasks events log at {events_log}"
    events_text = events_log.read_text()
    assert "(session ended)" in events_text, (
        "Expected the script to reach its gone-session exit log line "
        f"'... (session ended)'. Events log contents:\n{events_text}"
    )
