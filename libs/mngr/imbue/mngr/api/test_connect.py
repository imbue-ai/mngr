"""Integration tests for connect-related functionality."""

import importlib.resources
import os
import shlex
import signal
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

from imbue.mngr import resources as mngr_resources
from imbue.mngr.api.connect import SIGNAL_EXIT_CODE_STOP
from imbue.mngr.api.connect import _build_ssh_activity_wrapper_script
from imbue.mngr.hosts.host import build_host_tmux_config
from imbue.mngr.hosts.tmux import AGENT_ID_OPTION
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import terminal_tmux_server
from imbue.mngr.utils.testing import tmux_session_cleanup

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
        pytest.param(140, 40, 140, 39, id="real-client-minus-status-line"),
        pytest.param(2, 1, 80, 23, id="degenerate-client-floored-to-minimum"),
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
    pane to real attaching clients. The window gets the client's geometry minus the status line
    (which #{client_height} counts but the window cannot use); a degenerate (e.g. 2x1) client is
    floored to a usable minimum client of 80x24 first, so Claude Code's TUI still renders.
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
        # Pin the status line to one row so the expected heights below do not depend on the
        # test server's global `status` setting.
        subprocess.run(["tmux", "set-option", "-t", session_name, "status", "on"], check=True)
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


@contextmanager
def _remote_agent_session(session_name: str, agent_id: AgentId, host_dir: Path) -> Generator[None, None, None]:
    """A session set up the way a remote host's mngr leaves one: stamped with its agent id, under mngr's remote-host config."""
    config_path = host_dir / "tmux.conf"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(build_host_tmux_config(is_local=False, host_dir=host_dir, additional_config_path=None))
    subprocess.run(["tmux", "new-session", "-d", "-s", session_name, "-n", "agent", "sleep 73914"], check=True)
    try:
        subprocess.run(["tmux", "set-option", "-t", f"={session_name}:", AGENT_ID_OPTION, str(agent_id)], check=True)
        subprocess.run(["tmux", "source-file", str(config_path)], check=True)
        yield
    finally:
        cleanup_tmux_session(session_name)


@contextmanager
def _ssh_terminal_running_connect_wrapper(
    session_name: str, agent_id: AgentId, host_dir: Path, exit_code_file: Path
) -> Generator[str, None, None]:
    """Run ``mngr connect``'s remote wrapper attached to ``session_name`` in a terminal of its own.

    The terminal is a session on a second tmux server, standing in for the user's SSH terminal; it
    records the wrapper's exit code (what tells the local mngr to stop or destroy the agent).
    Yields the tmux socket name of that server.
    """
    wrapper = _build_ssh_activity_wrapper_script(session_name, agent_id, host_dir)
    terminal_command = (
        f"env -u TMUX bash -c {shlex.quote(wrapper)}; echo $? > {shlex.quote(str(exit_code_file))}; sleep 73915"
    )
    with terminal_tmux_server([terminal_command], width=120, height=40) as terminal_socket:
        wait_for(
            lambda: subprocess.run(
                ["tmux", "list-clients", "-t", f"={session_name}"], capture_output=True, text=True
            ).stdout.strip()
            != "",
            timeout=10.0,
            error_message="the connect wrapper never attached to the agent session",
        )
        yield terminal_socket


def _type_into_terminal(terminal_socket: str, *keys: str) -> None:
    subprocess.run(["tmux", "-L", terminal_socket, "send-keys", "-t", "=terminal:", *keys], check=True)


@pytest.mark.tmux
def test_remote_ctrl_t_stops_an_agent_renamed_while_attached(mngr_test_prefix: str, tmp_path: Path) -> None:
    """Ctrl-t on a remote attach must still tell the local mngr to stop the agent after the session was renamed under it."""
    session_name = f"{mngr_test_prefix}connect-{get_short_random_string()}"
    agent_id = AgentId(f"agent-{uuid4().hex}")
    host_dir = tmp_path / "host"
    exit_code_file = tmp_path / "wrapper_exit_code"
    renamed_session_name = f"{session_name}-renamed"

    with tmux_session_cleanup(renamed_session_name), _remote_agent_session(session_name, agent_id, host_dir):
        with _ssh_terminal_running_connect_wrapper(session_name, agent_id, host_dir, exit_code_file) as terminal:
            subprocess.run(["tmux", "rename-session", "-t", f"={session_name}", renamed_session_name], check=True)
            _type_into_terminal(terminal, "C-t")
            wait_for(lambda: exit_code_file.exists(), timeout=10.0, error_message="the connect wrapper did not exit")

    assert exit_code_file.read_text().strip() == str(SIGNAL_EXIT_CODE_STOP)


@pytest.mark.tmux
def test_a_signal_file_left_by_an_earlier_connect_does_not_stop_the_agent_on_a_plain_detach(
    mngr_test_prefix: str, tmp_path: Path
) -> None:
    """A stop signal an earlier connect never consumed (its wrapper looked under another name) must not fire later."""
    session_name = f"{mngr_test_prefix}connect-{get_short_random_string()}"
    agent_id = AgentId(f"agent-{uuid4().hex}")
    host_dir = tmp_path / "host"
    exit_code_file = tmp_path / "wrapper_exit_code"
    (host_dir / "signals").mkdir(parents=True)
    for leftover_key in (str(agent_id), session_name):
        (host_dir / "signals" / leftover_key).write_text("stop\n")

    with _remote_agent_session(session_name, agent_id, host_dir):
        with _ssh_terminal_running_connect_wrapper(session_name, agent_id, host_dir, exit_code_file) as terminal:
            _type_into_terminal(terminal, "C-b", "d")
            wait_for(lambda: exit_code_file.exists(), timeout=10.0, error_message="the connect wrapper did not exit")

    assert exit_code_file.read_text().strip() == "0"
    assert list((host_dir / "signals").iterdir()) == []


def _find_child_sleep_pid(parent_pid: int) -> int | None:
    return next((child.pid for child in psutil.Process(parent_pid).children() if child.name() == "sleep"), None)


@pytest.mark.tmux
def test_sigwinch_panes_script_run_is_unaffected_by_an_in_place_rewrite(mngr_test_prefix: str, tmp_path: Path) -> None:
    """Rewriting the script in place while a run sits in its delay must not change what that run executes.

    bash re-reads a script from its current offset between top-level commands, so the replacement
    puts a comment under every offset the run could resume at, followed by ``exit 71``.
    """
    script_path = tmp_path / "sigwinch_panes.sh"
    original_script = Path(_SIGWINCH_PANES_SCRIPT_PATH).read_bytes()
    script_path.write_bytes(original_script)
    process = subprocess.Popen(
        ["bash", str(script_path), f"{mngr_test_prefix}sigwinch-rewrite", "agent", "nudge"],
        env={**os.environ, "MNGR_SIGWINCH_DELAY_SECONDS": "73915"},
        start_new_session=True,
    )
    try:
        wait_for(
            lambda: _find_child_sleep_pid(process.pid) is not None,
            timeout=10.0,
            error_message="sigwinch_panes.sh did not reach its delay",
        )
        script_path.write_bytes(b"#" * len(original_script) + b"\nexit 71\n")
        sleep_pid = _find_child_sleep_pid(process.pid)
        assert sleep_pid is not None
        os.kill(sleep_pid, signal.SIGTERM)

        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
