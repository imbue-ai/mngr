"""Acceptance tests: urwid TUI terminal compatibility.

These tests verify that the urwid-based TUI (plugin install wizard) works
correctly with macOS kqueue when stdin is piped (the ``curl | bash`` install
path) and when run directly.

macOS kqueue does not support EVFILT_READ on ``/dev/tty`` (the virtual
controlling-terminal device), returning EINVAL.  It does work on the actual
pty device (e.g. ``/dev/ttys003``).  These tests guard against regressions
where the code opens ``/dev/tty`` instead of the real pty path.

These require a real terminal (tmux) because the bug manifests only when
kqueue interacts with actual device file descriptors -- mocks cannot catch it.
"""

import subprocess
from pathlib import Path

import pytest

from imbue.mngr.utils.polling import poll_until

_SENTINEL = "URWID_TTY_TEST_DONE"

_REPO_ROOT = str(Path(__file__).resolve().parents[4])


def _write_test_script(path: Path) -> None:
    """Write the kqueue test script to *path*.

    The script is built here rather than stored as a module-level string
    constant so that ratchet regexes (which scan raw source for patterns
    like ``import`` with leading whitespace) don't misfire on the embedded
    Python code.
    """
    lines = [
        "import os, sys, selectors, socket",
        "from imbue.mngr.cli.urwid_utils import _resolve_real_tty_path",
        "",
        "path = _resolve_real_tty_path()",
        'sys.stdout.write(f"resolved_tty_path={path}\\n")',
        "sys.stdout.flush()",
        "",
        "tty_file = open(path)",
        "",
        "rd, wr = socket.socketpair()",
        "rd.setblocking(False)",
        "",
        "sel = selectors.DefaultSelector()",
        "try:",
        "    sel.register(rd, selectors.EVENT_READ)",
        "    sel.register(tty_file, selectors.EVENT_READ)",
        '    sys.stdout.write("kqueue_register=OK\\n")',
        "except OSError as e:",
        '    sys.stdout.write(f"kqueue_register=FAILED: {e}\\n")',
        "finally:",
        "    sel.close()",
        "    rd.close()",
        "    wr.close()",
        "    tty_file.close()",
        "",
        f'sys.stdout.write("{_SENTINEL}\\n")',
        "sys.stdout.flush()",
    ]
    path.write_text("\n".join(lines) + "\n")


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

    poll_until(_sentinel_appeared, timeout=timeout, poll_interval=0.5)

    subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)
    return captured


@pytest.mark.acceptance
@pytest.mark.tmux
@pytest.mark.timeout(30)
def test_kqueue_tty_registration_with_piped_stdin(
    tmp_path: Path,
    _isolate_tmux_server: None,
) -> None:
    """kqueue can register the resolved tty path when stdin is piped.

    Regression test for the ``curl -fsSL ... | bash`` install path where
    stdin is a pipe, not a tty.
    """
    py_script = tmp_path / "kqueue_test.py"
    sh_script = tmp_path / "kqueue_test.sh"
    _write_test_script(py_script)
    _write_shell_wrapper(sh_script, py_script)

    # Pipe through bash (stdin becomes the pipe, not a tty)
    output = _run_in_tmux_and_capture(
        "kqueue-piped",
        f"cat {sh_script} | bash",
    )

    assert "kqueue_register=OK" in output, f"kqueue registration failed with piped stdin. Output:\n{output}"


@pytest.mark.acceptance
@pytest.mark.tmux
@pytest.mark.timeout(30)
def test_kqueue_tty_registration_with_direct_stdin(
    tmp_path: Path,
    _isolate_tmux_server: None,
) -> None:
    """kqueue can register the resolved tty path when stdin is a tty.

    Verifies that the normal (non-piped) execution path still works after
    the /dev/tty fix.
    """
    py_script = tmp_path / "kqueue_test.py"
    sh_script = tmp_path / "kqueue_test.sh"
    _write_test_script(py_script)
    _write_shell_wrapper(sh_script, py_script)

    # Run directly (stdin is the tty)
    output = _run_in_tmux_and_capture(
        "kqueue-direct",
        f"bash {sh_script}",
    )

    assert "kqueue_register=OK" in output, f"kqueue registration failed with direct stdin. Output:\n{output}"
