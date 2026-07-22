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


def _start_catcher_session(session_name: str, tmp_path: Path) -> tuple[Path, Path]:
    """Start a detached tmux SIGWINCH-catcher session and wait until its trap is installed.

    Creates a 200x50 session whose single ``agent`` window runs the catcher command,
    then blocks until the pane has written its ready file (so a later signal is not
    lost to bash's default WINCH action). Returns ``(marker_file, ready_file)``.
    """
    marker_file = tmp_path / "sigwinch_received"
    ready_file = tmp_path / "catcher_ready"
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
    wait_for(
        lambda: ready_file.exists(),
        timeout=5.0,
        error_message="catcher pane did not install its SIGWINCH trap",
    )
    return marker_file, ready_file


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

    try:
        marker_file, _ = _start_catcher_session(session_name, tmp_path)

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

    try:
        # Start the catcher (which waits for its WINCH trap to be installed) so that,
        # if the guard were (incorrectly) to signal, the trap would record it.
        marker_file, _ = _start_catcher_session(session_name, tmp_path)
        subprocess.run(
            ["tmux", "set-option", "-t", f"={session_name}:agent", "window-size", "manual"],
            check=True,
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


def _window_size(session_name: str) -> tuple[int, int]:
    """Return the (width, height) of the session's ``agent`` window."""
    result = subprocess.run(
        ["tmux", "list-windows", "-t", f"={session_name}", "-F", "#{window_name} #{window_width}x#{window_height}"],
        check=True,
        capture_output=True,
        text=True,
    )
    for line in result.stdout.splitlines():
        name, _, size = line.partition(" ")
        if name == "agent":
            width, _, height = size.partition("x")
            return int(width), int(height)
    raise AssertionError(f"no 'agent' window found in session {session_name!r}: {result.stdout!r}")


@pytest.mark.flaky
@pytest.mark.tmux
@pytest.mark.parametrize(
    "client_width, client_height, expected_width, expected_height",
    [
        pytest.param(140, 40, 140, 40, id="real-client-fits-exactly"),
        pytest.param(2, 1, 80, 24, id="degenerate-client-floored-to-minimum"),
    ],
)
def test_sigwinch_panes_script_fit_mode_resizes_pinned_window_to_client(
    mngr_test_prefix: str,
    tmp_path,
    client_width: int,
    client_height: int,
    expected_width: int,
    expected_height: int,
) -> None:
    """In "fit" mode the script re-fits the manual-pinned window to the attaching client.

    The default sizing policy pins the agent window to window-size=manual (so a degenerate
    client on the shared server can never collapse it) and relies on this hook to re-fit the
    pane to real attaching clients. A real client's geometry is honored exactly; a degenerate
    (e.g. 2x1) client is floored to a usable minimum (80x24) so Claude Code's TUI still renders.
    Fit mode also repaints unconditionally (unlike nudge's manual-pin guard).
    """
    session_name = f"{mngr_test_prefix}sigwinch-fit"

    try:
        # A resilient catcher: it re-enters `wait` after each trapped WINCH (unlike the shared
        # single-`wait` catcher, which exits on the signal and would destroy the session before
        # we read its window size). This lets us assert both the resize and the repaint.
        marker_file = tmp_path / "sigwinch_received"
        ready_file = tmp_path / "catcher_ready"
        catcher = (
            f"trap 'echo received > {marker_file}' WINCH; "
            f"echo ready > {ready_file}; "
            "while :; do sleep 3600 & wait; done"
        )
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
                catcher,
            ],
            check=True,
        )
        wait_for(
            lambda: ready_file.exists(),
            timeout=5.0,
            error_message="catcher pane did not install its SIGWINCH trap",
        )
        # Reproduce the default policy's pinned state so resize-window sticks.
        subprocess.run(
            ["tmux", "set-option", "-t", f"={session_name}:agent", "window-size", "manual"],
            check=True,
        )
        subprocess.run(
            ["tmux", "resize-window", "-t", f"={session_name}:agent", "-x", "200", "-y", "50"],
            check=True,
        )

        subprocess.run(
            [
                "bash",
                _SIGWINCH_PANES_SCRIPT_PATH,
                session_name,
                "agent",
                "fit",
                str(client_width),
                str(client_height),
            ],
            check=True,
            env=_no_delay_env(),
        )

        assert _window_size(session_name) == (expected_width, expected_height)
        wait_for(
            lambda: marker_file.exists(),
            timeout=5.0,
            error_message="fit mode should still deliver SIGWINCH to pane processes after resizing.",
        )

    finally:
        cleanup_tmux_session(session_name)
