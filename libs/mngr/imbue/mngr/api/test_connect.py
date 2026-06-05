"""Integration tests for connect-related functionality."""

import subprocess

import pytest

from imbue.mngr.api.connect import build_post_attach_resize_script
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session


@pytest.mark.flaky
@pytest.mark.tmux
def test_post_attach_resize_delivers_sigwinch_to_pane_process(mngr_test_prefix: str, tmp_path) -> None:
    """Verify build_post_attach_resize_script delivers SIGWINCH to pane processes.

    When connecting to a remote agent via SSH, the tmux session may have been
    created at 200x50 but the user's terminal is a different size. The resize
    script must deliver SIGWINCH to the agent process so it redraws.

    This test creates a session whose initial command is a SIGWINCH catcher,
    then runs the actual resize script from connect.py and verifies SIGWINCH
    was delivered. It is a regression test: the old approach (pkill -f with a
    process name pattern) failed on macOS where Claude's process title shows
    as its version number, and was dependent on a && chain that could silently
    skip the SIGWINCH step.
    """
    session_name = f"{mngr_test_prefix}sigwinch-connect"
    marker_file = tmp_path / "sigwinch_received"

    # Background sleep + wait allows bash to process traps when SIGWINCH
    # interrupts the wait builtin (plain sleep ignores SIGWINCH). The sleep
    # is one long-running child rather than a respawn loop so the child is
    # reliably alive when the resize script's pgrep-then-kill runs against
    # it -- mirroring real agent processes (e.g., `claude`) which are
    # long-lived, not a corpse that died between pgrep and kill.
    #
    # The trap is re-armed inside a loop: a SIGWINCH that interrupts `wait`
    # returns from it, so without the loop the script would exit after the
    # first signal and a single spurious/early SIGWINCH (e.g., delivered
    # before the trap is fully installed) could leave the marker unwritten.
    # Looping `wait` keeps the catcher alive to handle any later SIGWINCH the
    # resize script delivers, reducing spurious failures.
    catcher_cmd = (
        f"trap 'echo received > {marker_file}' WINCH; sleep 60 & while kill -0 $! 2>/dev/null; do wait $!; done"
    )

    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-x", "200", "-y", "50", "bash", "-c", catcher_cmd],
            check=True,
        )

        # Wait for the pane to be running
        wait_for(
            lambda: subprocess.run(["tmux", "has-session", "-t", session_name], capture_output=True).returncode == 0,
            timeout=5.0,
            error_message="tmux session did not start",
        )

        # Run the actual resize script from connect.py
        resize_script = build_post_attach_resize_script(session_name)
        subprocess.run(["bash", "-c", resize_script], check=True)

        wait_for(
            lambda: marker_file.exists(),
            timeout=10.0,
            error_message=(
                "SIGWINCH did not reach the pane process after running "
                "build_post_attach_resize_script. The resize mechanism should "
                "deliver SIGWINCH to pane processes."
            ),
        )

    finally:
        cleanup_tmux_session(session_name)
