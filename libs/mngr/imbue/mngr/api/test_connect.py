"""Integration tests for connect-related functionality."""

import importlib.resources
import os
import subprocess
from pathlib import Path

import pytest

from imbue.mngr import resources as mngr_resources
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session

# Path to the shipped SIGWINCH repaint script, resolved from package resources so the
# test exercises exactly what gets installed onto hosts.
_SIGWINCH_PANES_SCRIPT_PATH = str(importlib.resources.files(mngr_resources).joinpath("sigwinch_panes.sh"))


def _no_delay_env() -> dict[str, str]:
    """Environment that disables the script's self-delay so the nudge fires immediately.

    Built at call time (not module import) so it inherits the per-test TMUX_TMPDIR set
    by the autouse tmux-isolation fixture -- otherwise the script would target the
    default tmux server instead of the test's own.
    """
    return {**os.environ, "MNGR_SIGWINCH_DELAY_SECONDS": "0"}


# A pane command that records each SIGWINCH it receives by writing a marker file.
# It writes a ready file *after* installing the trap so the test can wait for the
# trap to be in place before signaling -- otherwise a SIGWINCH delivered before the
# trap is installed is lost to bash's default (ignore) action, making the test flaky.
# Background sleep + wait allows bash to process traps when SIGWINCH interrupts the
# wait builtin (plain sleep ignores SIGWINCH). The sleep is one long-running child
# rather than a respawn loop so the child is reliably alive when the script's
# pgrep-then-kill runs against it -- mirroring real agent processes (e.g. `claude`)
# which are long-lived, not a corpse that died between pgrep and kill.
def _build_sigwinch_catcher_command(marker_file: Path, ready_file: Path) -> str:
    return f"trap 'echo received > {marker_file}' WINCH; echo ready > {ready_file}; sleep 60 & wait"


@pytest.mark.flaky
@pytest.mark.tmux
def test_sigwinch_panes_script_delivers_to_pane_process(mngr_test_prefix: str, tmp_path) -> None:
    """Verify sigwinch_panes.sh delivers SIGWINCH to pane processes.

    A client may attach at a different size than the session was created at, leaving
    a stale, unpainted frame. The per-session client-attached hook runs this script
    to nudge the agent process to re-query its size (TIOCGWINSZ) and redraw. This
    test runs the script directly against a SIGWINCH-catcher session and verifies the
    signal reached the pane's child process.
    """
    session_name = f"{mngr_test_prefix}sigwinch-deliver"
    marker_file = tmp_path / "sigwinch_received"
    ready_file = tmp_path / "catcher_ready"

    try:
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-x",
                "200",
                "-y",
                "50",
                "-n",
                "agent",
                "bash",
                "-c",
                _build_sigwinch_catcher_command(marker_file, ready_file),
            ],
            check=True,
        )

        # Wait until the pane has installed its WINCH trap before signaling.
        wait_for(
            lambda: ready_file.exists(),
            timeout=5.0,
            error_message="catcher pane did not install its SIGWINCH trap",
        )

        subprocess.run(
            ["bash", _SIGWINCH_PANES_SCRIPT_PATH, session_name, "agent"],
            check=True,
            env=_no_delay_env(),
        )

        wait_for(
            lambda: marker_file.exists(),
            timeout=5.0,
            error_message=(
                "SIGWINCH did not reach the pane process after running sigwinch_panes.sh. "
                "The post-attach nudge should deliver SIGWINCH to pane processes."
            ),
        )

    finally:
        cleanup_tmux_session(session_name)


@pytest.mark.flaky
@pytest.mark.tmux
def test_sigwinch_panes_script_skips_pinned_window(mngr_test_prefix: str, tmp_path) -> None:
    """Verify sigwinch_panes.sh does not signal a pinned (window-size=manual) window.

    A manual-pinned window never resizes on attach, so there is nothing to repaint
    and the deliberately-fixed dimensions must be left untouched. The script's guard
    must short-circuit before signaling.
    """
    session_name = f"{mngr_test_prefix}sigwinch-pinned"
    marker_file = tmp_path / "sigwinch_received"
    ready_file = tmp_path / "catcher_ready"

    try:
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "-x",
                "200",
                "-y",
                "50",
                "-n",
                "agent",
                "bash",
                "-c",
                _build_sigwinch_catcher_command(marker_file, ready_file),
            ],
            check=True,
        )
        subprocess.run(
            ["tmux", "set-option", "-t", f"={session_name}:agent", "window-size", "manual"],
            check=True,
        )

        # Wait until the pane has installed its WINCH trap so that, if the guard were
        # (incorrectly) to signal, the trap would be in place to record it.
        wait_for(
            lambda: ready_file.exists(),
            timeout=5.0,
            error_message="catcher pane did not install its SIGWINCH trap",
        )

        # The script runs synchronously (delay disabled), so by the time it returns the
        # guard has already decided not to signal. The marker must therefore be absent.
        subprocess.run(
            ["bash", _SIGWINCH_PANES_SCRIPT_PATH, session_name, "agent"],
            check=True,
            env=_no_delay_env(),
        )
        assert not marker_file.exists(), (
            "sigwinch_panes.sh signaled a window-size=manual window, but the manual-pin guard should have skipped it."
        )

    finally:
        cleanup_tmux_session(session_name)
