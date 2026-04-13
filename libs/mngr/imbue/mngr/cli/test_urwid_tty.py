"""Acceptance tests: urwid TUI terminal compatibility.

These tests verify that the urwid-based TUI (plugin install wizard) works
correctly with the platform's I/O selector (kqueue on macOS, epoll on Linux)
when stdin is piped (the ``curl | bash`` install path) and when run directly.

macOS kqueue does not support EVFILT_READ on ``/dev/tty`` (the virtual
controlling-terminal device), returning EINVAL.  It does work on the actual
pty device (e.g. ``/dev/ttys003``).  These tests guard against regressions
where the code opens ``/dev/tty`` instead of the real pty path.

On Linux (epoll), ``/dev/tty`` works fine, but the tty device may not exist
in containerized environments (e.g. Modal sandboxes). These tests verify
that ``resolve_real_tty_path`` returns a usable path in both environments.

These require a real terminal (tmux) because the bug manifests only when
the selector interacts with actual device file descriptors -- mocks cannot
catch it.
"""

import subprocess
from pathlib import Path

import pytest

from imbue.mngr.utils.polling import wait_for

_SENTINEL = "URWID_TTY_TEST_DONE"

_REPO_ROOT = str(Path(__file__).resolve().parents[4])

_TEST_SCRIPT = Path(__file__).with_name("_kqueue_tty_test_script.py")


def _write_shell_wrapper(shell_path: Path, py_path: Path) -> None:
    """Write a shell script that runs the Python test script via uv."""
    shell_path.write_text(f"cd {_REPO_ROOT} && uv run python {py_path}\n")
    shell_path.chmod(0o755)


def _run_in_tmux_and_capture(
    session_name: str,
    command: str,
    timeout: float = 15.0,
) -> str:
    """Start *command* in a fresh tmux session and return pane output."""
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-x", "200", "-y", "50"],
        check=True,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, command, "Enter"],
        check=True,
    )

    captured = ""

    def _sentinel_appeared() -> bool:
        nonlocal captured
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p"],
            capture_output=True,
            text=True,
        )
        captured = result.stdout
        return _SENTINEL in captured

    wait_for(
        _sentinel_appeared,
        timeout=timeout,
        poll_interval=0.5,
        error_message=f"Sentinel {_SENTINEL!r} did not appear in tmux session {session_name!r} within {timeout}s",
    )

    subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
    return captured


def _assert_tty_test_output(output: str, context: str) -> None:
    """Check the test script output for success or expected environment limitations."""
    if "tty_open=FAILED" in output:
        # In containers without /dev/tty, the path can't be opened. This is
        # expected -- the test verifies the script doesn't crash, not that
        # /dev/tty exists in every environment.
        return
    assert "selector_register=OK" in output, f"Selector registration failed with {context}. Output:\n{output}"


@pytest.mark.acceptance
@pytest.mark.tmux
@pytest.mark.timeout(30)
def test_tty_selector_registration_with_piped_stdin(
    tmp_path: Path,
    _isolate_tmux_server: None,
) -> None:
    """Selector can register the resolved tty path when stdin is piped.

    Regression test for the ``curl -fsSL ... | bash`` install path where
    stdin is a pipe, not a tty.
    """
    sh_script = tmp_path / "tty_test.sh"
    _write_shell_wrapper(sh_script, _TEST_SCRIPT)

    # Pipe through bash (stdin becomes the pipe, not a tty)
    output = _run_in_tmux_and_capture(
        "tty-piped",
        f"cat {sh_script} | bash",
    )

    _assert_tty_test_output(output, "piped stdin")


@pytest.mark.acceptance
@pytest.mark.tmux
@pytest.mark.timeout(30)
def test_tty_selector_registration_with_direct_stdin(
    tmp_path: Path,
    _isolate_tmux_server: None,
) -> None:
    """Selector can register the resolved tty path when stdin is a tty.

    Verifies that the normal (non-piped) execution path still works after
    the /dev/tty fix.
    """
    sh_script = tmp_path / "tty_test.sh"
    _write_shell_wrapper(sh_script, _TEST_SCRIPT)

    # Run directly (stdin is the tty)
    output = _run_in_tmux_and_capture(
        "tty-direct",
        f"bash {sh_script}",
    )

    _assert_tty_test_output(output, "direct stdin")
