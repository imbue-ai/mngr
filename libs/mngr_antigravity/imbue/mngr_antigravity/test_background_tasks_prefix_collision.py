"""Regression test: antigravity_background_tasks.sh must exit promptly when its
target session is gone, even if a prefix-colliding sibling session is still alive.

Mirrors ``libs/mngr_claude/imbue/mngr_claude/test_background_tasks_prefix_collision.py``;
see that file for the design rationale.
"""

import importlib.resources
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.mngr.utils.testing import build_background_tasks_supervisor_env
from imbue.mngr.utils.testing import create_background_tasks_state_dir
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_antigravity import resources as _antigravity_resources


@pytest.fixture
def colliding_session_pair() -> Generator[tuple[str, str], None, None]:
    """Create two tmux sessions whose names share a prefix, then kill the shorter."""
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
    return create_background_tasks_state_dir(tmp_path / "state")


@pytest.mark.tmux
def test_antigravity_background_tasks_exits_when_target_session_is_gone_despite_prefix_collision(
    colliding_session_pair: tuple[str, str],
    state_dir_with_log_lib: Path,
) -> None:
    """The script must exit promptly when its named session is gone, even if a
    prefix-colliding sibling is still alive. See claude_background_tasks.sh's
    equivalent test for the failure mode this guards against.
    """
    stopped, _alive = colliding_session_pair

    env = build_background_tasks_supervisor_env(state_dir_with_log_lib, None)

    # With the fix the script exits in <1s. Without it, the script enters the
    # main loop (whose inner sleep is 15s) and never exits -- subprocess.run
    # surfaces this as TimeoutExpired and the test fails.
    script = importlib.resources.files(_antigravity_resources) / "antigravity_background_tasks.sh"
    with importlib.resources.as_file(script) as script_path:
        result = subprocess.run(
            ["bash", str(script_path), stopped],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )

    assert result.returncode == 0, (
        f"Background script exited non-zero: returncode={result.returncode}\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
