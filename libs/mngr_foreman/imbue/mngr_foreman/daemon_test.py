"""Tests for the background-daemon primitives (liveness, pid-file, detached spawn)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from imbue.mngr_foreman import daemon


def _dead_pid() -> int:
    """A PID that is (almost certainly) not alive: run a child, reap it, reuse its PID.

    ``os.kill(pid, 0)`` on a just-reaped PID reliably raises ``ProcessLookupError``
    in the tiny window before the number is recycled -- good enough for a test.
    Uses ``subprocess`` (not a raw fork) to avoid the fork-in-a-multithreaded-
    process warning under xdist.
    """
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def test_is_process_alive_for_self() -> None:
    assert daemon.is_process_alive(os.getpid()) is True


def test_is_process_alive_for_dead_pid() -> None:
    assert daemon.is_process_alive(_dead_pid()) is False


def test_is_process_alive_rejects_nonpositive() -> None:
    assert daemon.is_process_alive(0) is False
    assert daemon.is_process_alive(-1) is False


def test_read_running_daemon_pid_missing_file(tmp_path: Path) -> None:
    assert daemon.read_running_daemon_pid(tmp_path / "nope.pid") is None


def test_read_running_daemon_pid_alive(tmp_path: Path) -> None:
    pid_file = tmp_path / "foreman.pid"
    daemon.write_pid_file(pid_file, os.getpid())
    assert daemon.read_running_daemon_pid(pid_file) == os.getpid()


def test_read_running_daemon_pid_stale_is_none(tmp_path: Path) -> None:
    pid_file = tmp_path / "foreman.pid"
    daemon.write_pid_file(pid_file, _dead_pid())
    # A leftover pid-file from a crashed server must never look "running".
    assert daemon.read_running_daemon_pid(pid_file) is None


def test_read_running_daemon_pid_garbage_is_none(tmp_path: Path) -> None:
    pid_file = tmp_path / "foreman.pid"
    pid_file.write_text("not-a-number\n")
    assert daemon.read_running_daemon_pid(pid_file) is None


def test_write_pid_file_creates_parent_dirs(tmp_path: Path) -> None:
    pid_file = tmp_path / "nested" / "dir" / "foreman.pid"
    daemon.write_pid_file(pid_file, 4242)
    assert pid_file.read_text().strip() == "4242"


def test_is_daemon_child_reads_env_marker(monkeypatch) -> None:
    monkeypatch.delenv("MNGR_FOREMAN_DAEMONIZED", raising=False)
    assert daemon.is_daemon_child() is False
    monkeypatch.setenv("MNGR_FOREMAN_DAEMONIZED", "1")
    assert daemon.is_daemon_child() is True


def test_spawn_detached_redirects_output_and_sets_marker(tmp_path: Path) -> None:
    """A detached child runs, its stdout lands in the log, and the marker is set.

    Uses the current interpreter as the "mngr binary" and a tiny inline program
    as the forwarded args, so the test needs no real mngr install.
    """
    log_file = tmp_path / "out.log"
    program = "import os,sys; sys.stdout.write('MARK=' + os.environ.get('MNGR_FOREMAN_DAEMONIZED', '') + '\\n')"
    pid = daemon.spawn_detached_foreman(
        mngr_binary=sys.executable,
        forwarded_args=("-c", program),
        log_file=log_file,
    )
    assert pid > 0
    # The detached child keeps the same parent (start_new_session changes the
    # session, not the parent), so we can reap it directly and read its log once
    # it has exited -- no polling/sleep needed. ChildProcessError => already reaped.
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    assert "MARK=1" in log_file.read_text()
