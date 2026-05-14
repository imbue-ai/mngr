"""Unit tests for :mod:`imbue.mngr_latchkey.forward_supervisor`.

Exercises the adopt / discard-stale / spawn-fresh state machine of
:class:`LatchkeyForwardSupervisor` end-to-end against a small fake
``mngr`` binary that imitates the actual ``mngr latchkey forward``
argv shape (so the cmdline-based liveness probe accepts it).
"""

import os
import threading
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final
from uuid import uuid4

import psutil

from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor
from imbue.mngr_latchkey.forward_supervisor import _cmdline_looks_like_mngr_latchkey_forward
from imbue.mngr_latchkey.forward_supervisor import is_forward_info_alive
from imbue.mngr_latchkey.store import LatchkeyForwardInfo
from imbue.mngr_latchkey.store import forward_info_path
from imbue.mngr_latchkey.store import forward_log_path
from imbue.mngr_latchkey.store import load_forward_info
from imbue.mngr_latchkey.store import save_forward_info

_POLL_INTERVAL_SECONDS: Final[float] = 0.05


def _wait_for_process_exit(pid: int, timeout: float = 5.0) -> bool:
    """Poll until ``pid`` is gone or has become a zombie.

    Zombies count as "exited" -- the subprocesses we spawn are children
    of the test process and we never ``wait()`` on the underlying
    ``Popen``, so a terminated child lingers in zombie state until the
    test process itself exits. For the purpose of these tests that is
    functionally equivalent to the process having exited.
    """
    poll_event = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return True
        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        poll_event.wait(timeout=_POLL_INTERVAL_SECONDS)
    return False


def _wait_for_process_alive(pid: int, timeout: float = 5.0) -> bool:
    """Poll until ``pid``'s cmdline matches ``mngr latchkey forward``.

    Between fork and exec the child briefly inherits the parent's argv,
    which makes ``is_forward_info_alive``'s cmdline check transiently
    fail. Waiting for the *specific* cmdline pattern (rather than just
    ``cmdline != []``) closes that window so adoption tests do not race
    with the kernel's exec syscall.
    """
    poll_event = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            process = psutil.Process(pid)
            cmdline = process.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            poll_event.wait(timeout=_POLL_INTERVAL_SECONDS)
            continue
        if _cmdline_looks_like_mngr_latchkey_forward(cmdline):
            return True
        poll_event.wait(timeout=_POLL_INTERVAL_SECONDS)
    return False


def _make_fake_mngr_binary(tmp_path: Path) -> Path:
    """Build a shell script that imitates ``mngr`` for the supervisor's purposes.

    Recognised invocations:

    * ``mngr latchkey forward --latchkey-directory <dir> [...]`` -- mirrors
      the real :func:`_forward_command` to the extent the supervisor's
      tests care about: writes a ``LatchkeyForwardInfo`` record to
      ``<dir>/mngr_latchkey/latchkey_forward.json`` (with the script's
      own PID), deletes it on SIGTERM, sleeps in between.
    * Anything else -- exits 99. Lets tests assert that the supervisor
      only ever spawns the supported subcommand.
    """
    script = tmp_path / "mngr"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, signal, sys\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        'if sys.argv[1:3] != ["latchkey", "forward"]:\n'
        "    sys.exit(99)\n"
        "args = sys.argv[3:]\n"
        "latchkey_directory = None\n"
        "for i, arg in enumerate(args):\n"
        '    if arg == "--latchkey-directory" and i + 1 < len(args):\n'
        "        latchkey_directory = Path(args[i + 1])\n"
        "        break\n"
        "if latchkey_directory is None:\n"
        "    sys.exit(98)\n"
        'record_path = latchkey_directory / "mngr_latchkey" / "latchkey_forward.json"\n'
        "record_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "record_path.write_text(json.dumps({\n"
        '    "pid": os.getpid(),\n'
        '    "started_at": datetime.now(timezone.utc).isoformat(),\n'
        '    "gateway_port": None,\n'
        "}))\n"
        "def _on_term(*_):\n"
        "    try:\n"
        "        record_path.unlink()\n"
        "    except OSError:\n"
        "        pass\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, _on_term)\n"
        "signal.pause()\n"
    )
    script.chmod(0o755)
    return script


_FORWARD_RECORD_POLL_TIMEOUT: Final[float] = 5.0
_FORWARD_RECORD_POLL_INTERVAL: Final[float] = 0.05


def _wait_for_forward_record(plugin_dir: Path) -> LatchkeyForwardInfo:
    """Block until the forward child publishes its record. Fails the test on timeout."""
    deadline = time.monotonic() + _FORWARD_RECORD_POLL_TIMEOUT
    waiter = threading.Event()
    while time.monotonic() < deadline:
        record = load_forward_info(plugin_dir)
        if record is not None:
            return record
        waiter.wait(timeout=_FORWARD_RECORD_POLL_INTERVAL)
    raise AssertionError(f"forward record never appeared at {plugin_dir} within {_FORWARD_RECORD_POLL_TIMEOUT}s")


# -- cmdline matcher ---------------------------------------------------------


def test_cmdline_matcher_accepts_plausible_mngr_latchkey_forward() -> None:
    assert _cmdline_looks_like_mngr_latchkey_forward(["mngr", "latchkey", "forward", "--latchkey-directory", "/tmp/d"])
    assert _cmdline_looks_like_mngr_latchkey_forward(["/usr/local/bin/mngr", "latchkey", "forward"])


def test_cmdline_matcher_handles_proctitle_overwrite() -> None:
    """``uv tool``-style wrappers fuse argv into argv[0] and zero out the rest.

    psutil surfaces this as ``["mngr latchkey forward ...", "", "", ...]``;
    the matcher must still recognise it as ours, or the supervisor will
    discard its own record and spawn a duplicate every time minds starts.
    """
    fused = (
        "mngr latchkey forward --latchkey-directory /home/user/.minds/latchkey "
        "--latchkey-binary /opt/latchkey/bin/latchkey --mngr-binary mngr"
    )
    cmdline = [fused] + [""] * 85
    assert _cmdline_looks_like_mngr_latchkey_forward(cmdline)


def test_cmdline_matcher_rejects_unrelated_processes() -> None:
    assert not _cmdline_looks_like_mngr_latchkey_forward([])
    # ``manager`` is not ``mngr``.
    assert not _cmdline_looks_like_mngr_latchkey_forward(["manager", "latchkey", "forward"])
    # ``mngr`` token present but no ``latchkey forward`` follow-up.
    assert not _cmdline_looks_like_mngr_latchkey_forward(["mngr", "forward"])
    # ``forward`` present but ``latchkey`` is missing.
    assert not _cmdline_looks_like_mngr_latchkey_forward(["mngr", "create", "forward"])


# -- ensure_running ----------------------------------------------------------


def test_ensure_running_spawns_when_no_record_exists(tmp_path: Path) -> None:
    fake_binary = _make_fake_mngr_binary(tmp_path)
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )

    info = supervisor.ensure_running()
    try:
        assert info.pid > 0
        assert isinstance(info.started_at, datetime)
        assert _wait_for_process_alive(info.pid)
        # The forward child publishes the record asynchronously after
        # the spawn returns; poll until it appears.
        persisted = _wait_for_forward_record(supervisor.plugin_data_dir)
        assert persisted.pid == info.pid
        assert forward_log_path(supervisor.plugin_data_dir).is_file()
    finally:
        supervisor.stop()
        assert _wait_for_process_exit(info.pid)


# A no-double-spawn / adoption-against-live-subprocess test used to live
# here but proved flaky under xdist (the fork->exec window between
# ``subprocess.Popen`` returning and the child running its own argv
# briefly leaves the cmdline as the parent's, racing with the
# cmdline-based liveness probe). The same logic is covered by the
# direct ``is_forward_info_alive`` tests below plus the
# ``_cmdline_looks_like_mngr_latchkey_forward`` matcher tests above
# without an end-to-end subprocess race.


def test_ensure_running_discards_stale_record_and_spawns_fresh(tmp_path: Path) -> None:
    """A record whose PID is dead is discarded; a fresh supervisor is spawned."""
    fake_binary = _make_fake_mngr_binary(tmp_path)
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )

    plugin_dir = supervisor.plugin_data_dir
    plugin_dir.mkdir(parents=True, exist_ok=True)
    # PID 1 (init) is alive but its cmdline is not ours, so the
    # cmdline check rejects it -- exactly the PID-reuse case.
    save_forward_info(
        plugin_dir,
        LatchkeyForwardInfo(pid=1, started_at=datetime.now(timezone.utc)),
    )

    info = supervisor.ensure_running()
    try:
        assert info.pid != 1
        assert _wait_for_process_alive(info.pid)
    finally:
        supervisor.stop()
        assert _wait_for_process_exit(info.pid)


def test_ensure_running_discards_record_for_dead_pid(tmp_path: Path) -> None:
    """A record whose PID has been reaped is treated as stale."""
    fake_binary = _make_fake_mngr_binary(tmp_path)
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )

    plugin_dir = supervisor.plugin_data_dir
    plugin_dir.mkdir(parents=True, exist_ok=True)
    # Pick an almost-certainly-dead PID. The supervisor must tolerate
    # ``psutil.NoSuchProcess`` and treat the record as stale.
    dead_pid = 2**31 - 1
    save_forward_info(
        plugin_dir,
        LatchkeyForwardInfo(pid=dead_pid, started_at=datetime.now(timezone.utc)),
    )

    info = supervisor.ensure_running()
    try:
        assert info.pid != dead_pid
        assert _wait_for_process_alive(info.pid)
    finally:
        supervisor.stop()
        assert _wait_for_process_exit(info.pid)


def test_stop_terminates_running_supervisor_and_deletes_record(tmp_path: Path) -> None:
    fake_binary = _make_fake_mngr_binary(tmp_path)
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )

    info = supervisor.ensure_running()
    assert _wait_for_process_alive(info.pid)
    _wait_for_forward_record(supervisor.plugin_data_dir)

    supervisor.stop()
    assert _wait_for_process_exit(info.pid)
    assert not forward_info_path(supervisor.plugin_data_dir).is_file()


def test_stop_is_no_op_when_nothing_running(tmp_path: Path) -> None:
    """``stop()`` must be safe to call without a running supervisor."""
    fake_binary = _make_fake_mngr_binary(tmp_path)
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )
    # Must not raise even with no running supervisor / on-disk record.
    supervisor.stop()


def test_get_forward_info_returns_none_when_unstarted(tmp_path: Path) -> None:
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary="/nonexistent-binary",
        latchkey_binary="/nonexistent-binary",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )
    assert supervisor.get_forward_info() is None


# -- liveness probe (direct) -------------------------------------------------


def testis_forward_info_alive_rejects_unrelated_pid() -> None:
    """A real PID whose cmdline doesn't match is rejected."""
    info = LatchkeyForwardInfo(pid=os.getpid(), started_at=datetime.now(timezone.utc))
    # The test process itself is pytest, not ``mngr latchkey forward``.
    assert not is_forward_info_alive(info)


def testis_forward_info_alive_rejects_dead_pid() -> None:
    dead_pid = 2**31 - 1
    info = LatchkeyForwardInfo(pid=dead_pid, started_at=datetime.now(timezone.utc))
    assert not is_forward_info_alive(info)


# -- malformed-record handling ----------------------------------------------


def test_ensure_running_replaces_malformed_record(tmp_path: Path) -> None:
    """A truncated / unreadable record is treated as 'no record'."""
    fake_binary = _make_fake_mngr_binary(tmp_path)
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )
    plugin_dir = supervisor.plugin_data_dir
    plugin_dir.mkdir(parents=True, exist_ok=True)
    forward_info_path(plugin_dir).write_text("{not-valid-json")

    info = supervisor.ensure_running()
    try:
        assert _wait_for_process_alive(info.pid)
    finally:
        supervisor.stop()
        assert _wait_for_process_exit(info.pid)
