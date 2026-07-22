"""Integration tests for connect-related functionality."""

import fcntl
import importlib.resources
import os
import pty
import struct
import subprocess
import termios
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


def _launch_catcher_session(session_name: str, catcher_command: str, ready_file: Path) -> None:
    """Start a detached 200x50 tmux session running the catcher and wait until its trap is installed.

    The session's single ``agent`` window runs the catcher command; this blocks until
    the pane has written its ready file (so a later signal is not lost to bash's
    default WINCH action).
    """
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
            catcher_command,
        ],
        check=True,
    )
    wait_for(
        lambda: ready_file.exists(),
        timeout=5.0,
        error_message="catcher pane did not install its SIGWINCH trap",
    )


def _start_catcher_session(session_name: str, tmp_path: Path) -> tuple[Path, Path]:
    """Start a detached tmux SIGWINCH-catcher session and wait until its trap is installed.

    Returns ``(marker_file, ready_file)``.
    """
    marker_file = tmp_path / "sigwinch_received"
    ready_file = tmp_path / "catcher_ready"
    _launch_catcher_session(session_name, _build_sigwinch_catcher_command(marker_file, ready_file), ready_file)
    return marker_file, ready_file


def _start_pinned_resilient_catcher_session(session_name: str, tmp_path: Path) -> Path:
    """Start a manual-pinned 200x50 catcher session whose pane survives repeated SIGWINCH.

    Unlike ``_start_catcher_session``'s single-``wait`` catcher (which exits on the first
    trapped signal, destroying the session), this catcher re-enters ``wait`` after each
    trapped WINCH, so the script's repaint signal cannot kill the pane -- and with it the
    session -- before the window size is read back. The single ``agent`` window is pinned
    to window-size=manual to reproduce the default fit-mode policy (so resize-window
    sticks). Returns the marker file the trap writes on each received WINCH.
    """
    marker_file = tmp_path / "sigwinch_received"
    ready_file = tmp_path / "catcher_ready"
    catcher = (
        f"trap 'echo received > {marker_file}' WINCH; echo ready > {ready_file}; while :; do sleep 3600 & wait; done"
    )
    _launch_catcher_session(session_name, catcher, ready_file)
    subprocess.run(
        ["tmux", "set-option", "-t", f"={session_name}:agent", "window-size", "manual"],
        check=True,
    )
    return marker_file


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
        # The resilient catcher lets us assert both the resize and the repaint (a
        # single-`wait` catcher would exit on the signal, destroying the session
        # before we read its window size back).
        marker_file = _start_pinned_resilient_catcher_session(session_name, tmp_path)
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


@pytest.mark.flaky
@pytest.mark.tmux
def test_sigwinch_panes_script_fit_mode_converges_on_live_client_size(mngr_test_prefix: str, tmp_path) -> None:
    """Fit mode resizes to the session's LIVE client size, not the hook-fire arguments.

    A resize burst (e.g. a sash drag in the web terminal) fires many overlapping hook
    instances, each carrying the client geometry captured when its hook fired. If the
    script trusted those arguments, whichever backgrounded instance's resize-window
    landed last could pin the manual window at a stale intermediate size. The script
    must instead read the current client size at act time, so any instance -- here one
    invoked with deliberately stale arguments -- converges the window on the real size.
    """
    session_name = f"{mngr_test_prefix}sigwinch-live"
    client_width, client_height = 143, 37

    master_fd = None
    attach_proc = None
    try:
        # The resilient catcher keeps the session alive through the script's repaint
        # signal; this test ignores the returned repaint marker (asserted elsewhere).
        _start_pinned_resilient_catcher_session(session_name, tmp_path)

        # Attach a real client through a pty pre-sized to the target geometry. The
        # attach must not run inside any ambient tmux (nested-session guard).
        master_fd, slave_fd = pty.openpty()
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", client_height, client_width, 0, 0))
        attach_env = {**os.environ}
        attach_env.pop("TMUX", None)
        # A capable TERM is required or the client exits with "open terminal failed"
        # (the test shell's TERM may be dumb/unset).
        attach_env["TERM"] = "xterm-256color"
        attach_proc = subprocess.Popen(
            ["tmux", "attach", "-t", f"={session_name}"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            env=attach_env,
        )
        os.close(slave_fd)
        os.set_blocking(master_fd, False)

        def _client_attached() -> bool:
            # Drain the pty so the tmux client never blocks on a full buffer.
            try:
                while os.read(master_fd, 65536):
                    pass
            except BlockingIOError:
                pass
            except OSError:
                pass
            result = subprocess.run(
                ["tmux", "list-clients", "-t", f"={session_name}", "-F", "#{client_width}x#{client_height}"],
                capture_output=True,
                text=True,
            )
            return f"{client_width}x{client_height}" in result.stdout

        wait_for(
            lambda: _client_attached(),
            timeout=5.0,
            error_message="tmux client did not attach at the pty's size",
        )

        # Invoke the script with stale hook-fire geometry; it must ignore it in favor
        # of the live client.
        subprocess.run(
            ["bash", _SIGWINCH_PANES_SCRIPT_PATH, session_name, "agent", "fit", "90", "30"],
            check=True,
            env=_no_delay_env(),
        )
        assert _window_size(session_name) == (client_width, client_height)

    finally:
        if attach_proc is not None:
            attach_proc.terminate()
            try:
                attach_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # A client wedged (e.g. blocked writing to the no-longer-drained pty)
                # must not leak the fd/session below or mask the original failure.
                attach_proc.kill()
                attach_proc.wait(timeout=5)
        if master_fd is not None:
            os.close(master_fd)
        cleanup_tmux_session(session_name)
