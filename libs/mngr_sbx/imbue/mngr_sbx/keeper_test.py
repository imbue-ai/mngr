"""Unit tests for the sbx sandbox keeper helpers.

The subprocess-spawning path is not exercised here (it would require sbx + a
real sandbox); we cover the file-on-disk lifecycle and the pid-liveness check.
"""

import os
from pathlib import Path

from imbue.mngr.primitives import HostId
from imbue.mngr_sbx.keeper import is_keeper_alive
from imbue.mngr_sbx.keeper import keeper_log_path
from imbue.mngr_sbx.keeper import keeper_pid_path
from imbue.mngr_sbx.keeper import read_keeper_pid
from imbue.mngr_sbx.keeper import setup_keeper_command
from imbue.mngr_sbx.keeper import sshd_keeper_command
from imbue.mngr_sbx.keeper import stop_keeper


def test_keeper_pid_path_is_under_provider_dir(tmp_path: Path) -> None:
    host_id = HostId.generate()
    path = keeper_pid_path(tmp_path, host_id)
    assert path == tmp_path / "keepers" / f"{host_id}.pid"


def test_keeper_log_path_is_under_provider_dir(tmp_path: Path) -> None:
    host_id = HostId.generate()
    path = keeper_log_path(tmp_path, host_id)
    assert path == tmp_path / "keepers" / f"{host_id}.log"


def test_is_keeper_alive_returns_true_for_current_process() -> None:
    assert is_keeper_alive(os.getpid()) is True


def test_is_keeper_alive_returns_false_for_definitely_dead_pid() -> None:
    # PID 0 is special (the process group) and should never count as alive for our purposes.
    assert is_keeper_alive(0) is False


def test_is_keeper_alive_returns_false_for_unallocated_high_pid() -> None:
    # Pick a PID well above the system's pid_max ceiling. On macOS pid_max is 99998;
    # on Linux it's typically 32768. 2**31 - 2 is safely beyond either.
    assert is_keeper_alive(2_147_483_646) is False


def test_read_keeper_pid_returns_none_when_pidfile_missing(tmp_path: Path) -> None:
    assert read_keeper_pid(tmp_path, HostId.generate()) is None


def test_read_keeper_pid_returns_stored_int(tmp_path: Path) -> None:
    host_id = HostId.generate()
    path = keeper_pid_path(tmp_path, host_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("4242\n")
    assert read_keeper_pid(tmp_path, host_id) == 4242


def test_read_keeper_pid_returns_none_for_malformed_pidfile(tmp_path: Path) -> None:
    host_id = HostId.generate()
    path = keeper_pid_path(tmp_path, host_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-an-integer\n")
    assert read_keeper_pid(tmp_path, host_id) is None


def test_stop_keeper_removes_pidfile_when_pid_is_dead(tmp_path: Path) -> None:
    host_id = HostId.generate()
    path = keeper_pid_path(tmp_path, host_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # An unallocated PID counts as not-alive, so stop_keeper should clean up without signaling.
    path.write_text("2147483646\n")

    stop_keeper(tmp_path, host_id, timeout_seconds=1.0)

    assert not path.exists()


def test_stop_keeper_is_noop_when_pidfile_missing(tmp_path: Path) -> None:
    # Should not raise. Just confirm no file appears as a side effect.
    stop_keeper(tmp_path, HostId.generate(), timeout_seconds=1.0)
    assert not (tmp_path / "keepers").exists() or list((tmp_path / "keepers").iterdir()) == []


def test_setup_keeper_command_is_a_long_sleep() -> None:
    # The setup keeper must hold the sandbox alive for at least one day so it outlives any
    # reasonable mngr session that might be left running unattended during host creation.
    cmd = setup_keeper_command()
    assert cmd[0] == "sleep"
    assert int(cmd[1]) >= 86_400


def test_sshd_keeper_command_runs_foreground_sshd() -> None:
    cmd = sshd_keeper_command()
    # The wrapper script must launch /usr/sbin/sshd -D so the foreground process IS sshd.
    joined = " ".join(cmd)
    assert "/usr/sbin/sshd" in joined
    assert "-D" in joined
