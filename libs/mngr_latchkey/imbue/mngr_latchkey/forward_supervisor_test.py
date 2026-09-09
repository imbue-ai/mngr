"""Unit tests for :mod:`imbue.mngr_latchkey.forward_supervisor`.

Exercises the adopt / spawn-fresh / reap state machine of
:class:`LatchkeyForwardSupervisor` end-to-end against a small fake ``mngr``
binary that takes the same ownership lock the real ``mngr latchkey forward``
takes, so it is bound by the same one-forward-per-directory invariant.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Final
from uuid import uuid4

import psutil
import pytest

from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor
from imbue.mngr_latchkey.forward_supervisor import _descendant_processes
from imbue.mngr_latchkey.forward_supervisor import is_forward_owned_by
from imbue.mngr_latchkey.forward_supervisor import owning_forward_process
from imbue.mngr_latchkey.store import LatchkeyForwardOwner
from imbue.mngr_latchkey.store import acquire_forward_lock
from imbue.mngr_latchkey.store import forward_info_path
from imbue.mngr_latchkey.store import forward_lock_path
from imbue.mngr_latchkey.store import forward_log_path
from imbue.mngr_latchkey.store import forward_owner_path
from imbue.mngr_latchkey.store import load_forward_owner
from imbue.mngr_latchkey.store import plugin_data_dir
from imbue.mngr_latchkey.store import update_forward_owner_gateway_port

_POLL_INTERVAL_SECONDS: Final[float] = 0.05


# Upper bound for the process-state polls below. Purely a worst-case ceiling
# (every poll returns as soon as its condition holds): spawning and tearing
# down real subprocesses has been seen to exceed a 5s bound on a heavily
# loaded machine, which is noise, not a bug in the code under test.
_PROCESS_WAIT_TIMEOUT_SECONDS = 15.0


def _wait_for_process_exit(pid: int, timeout: float = _PROCESS_WAIT_TIMEOUT_SECONDS) -> bool:
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


def _wait_for_process_alive(pid: int, timeout: float = _PROCESS_WAIT_TIMEOUT_SECONDS) -> bool:
    """Poll until ``pid`` is a running process."""
    poll_event = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        poll_event.wait(timeout=_POLL_INTERVAL_SECONDS)
    return False


def _make_fake_mngr_binary(tmp_path: Path) -> Path:
    """Build a shell script that imitates ``mngr`` for the supervisor's purposes.

    Recognised invocations:

    * ``mngr latchkey forward --latchkey-directory <dir> [...]`` -- mirrors
      the real :func:`_forward_command` to the extent the supervisor's tests
      care about: takes the directory's ownership lock (exiting 97 when
      another already holds it), which records it as the directory's owner,
      then idles until SIGTERM.
    * Anything else -- exits 99. Lets tests assert that the supervisor
      only ever spawns the supported subcommand.
    """
    script = tmp_path / "mngr"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys, threading\n"
        "from pathlib import Path\n"
        "from imbue.mngr_latchkey.store import acquire_forward_lock\n"
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
        'plugin_dir = latchkey_directory / "mngr_latchkey"\n'
        "plugin_dir.mkdir(parents=True, exist_ok=True)\n"
        # Take the same ownership lock the real forward takes, so the fake is
        # bound by the real one-forward-per-directory invariant.
        "_forward_lock = acquire_forward_lock(plugin_dir)\n"
        "if _forward_lock is None:\n"
        "    sys.exit(97)\n"
        # Record the working directory the supervisor launched us in so a test
        # can assert the `cwd` field is threaded through to the spawn.
        '(plugin_dir / "observed_cwd.txt").write_text(os.getcwd())\n'
        # Written last, so a test that waits for it knows this fake has finished
        # claiming the directory and stamping itself as its owner.
        '(plugin_dir / f"ready_{os.getpid()}.txt").write_text("ready")\n'
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        # Mirror the real forward's SIGHUP observe-bounce handler with a
        # delivery sentinel, so bounce tests can assert whether the signal was
        # actually delivered (inherited dispositions make death-on-SIGHUP an
        # unreliable delivery signal across test environments).
        "def _on_hup(*_):\n"
        '    (plugin_dir / f"sighup_received_{os.getpid()}.txt").write_text("1")\n'
        "signal.signal(signal.SIGHUP, _on_hup)\n"
        # Block forever (handlers still run) rather than ``signal.pause()``,
        # which would fall through and exit after the first handled SIGHUP.
        "threading.Event().wait()\n"
    )
    script.chmod(0o755)
    return script


_FORWARD_RECORD_POLL_TIMEOUT: Final[float] = 5.0
_FORWARD_RECORD_POLL_INTERVAL: Final[float] = 0.05


def _wait_for_legacy_forward_record(plugin_dir: Path) -> None:
    """Block until a pre-lock fake publishes its ``latchkey_forward.json``.

    CLEANUP: delete with ``_pre_lock_migration``.
    """
    deadline = time.monotonic() + _FORWARD_RECORD_POLL_TIMEOUT
    waiter = threading.Event()
    while time.monotonic() < deadline:
        if forward_info_path(plugin_dir).is_file():
            return
        waiter.wait(timeout=_FORWARD_RECORD_POLL_INTERVAL)
    raise AssertionError(f"legacy forward record never appeared at {plugin_dir}")


def _wait_for_forward_record(plugin_dir: Path) -> LatchkeyForwardOwner:
    """Block until the forward child owns its directory. Fails the test on timeout."""
    deadline = time.monotonic() + _FORWARD_RECORD_POLL_TIMEOUT
    waiter = threading.Event()
    while time.monotonic() < deadline:
        record = load_forward_owner(plugin_dir)
        if record is not None:
            return record
        waiter.wait(timeout=_FORWARD_RECORD_POLL_INTERVAL)
    raise AssertionError(f"forward record never appeared at {plugin_dir} within {_FORWARD_RECORD_POLL_TIMEOUT}s")


def _wait_for_forward_ready(plugin_dir: Path, pid: int) -> None:
    """Block until the fake forward ``pid`` has dropped its per-pid ready sentinel.

    The sentinel (``ready_<pid>.txt``) is the fake's last write, so its presence
    proves that fake has claimed the directory and stamped itself as the owner.
    A test that stamps a gateway port onto that record has to wait for it, or it
    stamps a record that is not yet there. Fails the test on timeout.
    """
    sentinel = plugin_dir / f"ready_{pid}.txt"
    deadline = time.monotonic() + _FORWARD_RECORD_POLL_TIMEOUT
    waiter = threading.Event()
    while time.monotonic() < deadline:
        if sentinel.exists():
            return
        waiter.wait(timeout=_FORWARD_RECORD_POLL_INTERVAL)
    raise AssertionError(f"forward pid {pid} never wrote {sentinel} within {_FORWARD_RECORD_POLL_TIMEOUT}s")


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
        assert _wait_for_process_alive(info.pid)
        persisted = _wait_for_forward_record(supervisor.plugin_data_dir)
        assert persisted.pid == info.pid
        assert forward_log_path(supervisor.plugin_data_dir).is_file()
    finally:
        supervisor.stop()
        assert _wait_for_process_exit(info.pid)


def test_ensure_running_spawns_forward_in_configured_cwd(tmp_path: Path) -> None:
    """The ``cwd`` field is threaded through to the detached forward process.

    minds passes ``$HOME`` so the supervisor (a laptop-side mngr invocation)
    does not resolve project config from a transient cwd. Here we point it at a
    throwaway directory and assert the spawned child actually ran there.
    """
    fake_binary = _make_fake_mngr_binary(tmp_path)
    spawn_cwd = tmp_path / "spawn-cwd"
    spawn_cwd.mkdir()
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
        cwd=spawn_cwd,
    )

    info = supervisor.ensure_running()
    try:
        # The fake writes its cwd after claiming the directory, so waiting for
        # the claim alone would race the write this reads.
        _wait_for_forward_ready(supervisor.plugin_data_dir, info.pid)
        observed_cwd = (supervisor.plugin_data_dir / "observed_cwd.txt").read_text()
        # Resolve both sides: macOS routes tmp through a /private symlink, so the
        # child's getcwd() can differ textually from the path we passed.
        assert Path(observed_cwd).resolve() == spawn_cwd.resolve()
    finally:
        supervisor.stop()
        assert _wait_for_process_exit(info.pid)


def test_bounce_starts_supervisor_when_none_running(tmp_path: Path) -> None:
    """``bounce()`` with no live supervisor brings one up (start-if-down)."""
    fake_binary = _make_fake_mngr_binary(tmp_path)
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )

    # No record exists yet, so bounce must spawn rather than no-op.
    supervisor.bounce()
    try:
        persisted = _wait_for_forward_record(supervisor.plugin_data_dir)
        assert persisted.pid > 0
        assert _wait_for_process_alive(persisted.pid)
    finally:
        supervisor.stop()


def test_stop_terminates_running_supervisor_and_leaves_the_directory_unowned(tmp_path: Path) -> None:
    """What ``stop()`` has to leave behind is a directory the next spawn can claim."""
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
    assert owning_forward_process(supervisor.plugin_data_dir) is None


def test_stop_is_no_op_when_nothing_running(tmp_path: Path) -> None:
    """``stop()`` must be safe to call without a running supervisor."""
    fake_binary = _make_fake_mngr_binary(tmp_path)
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )
    supervisor.stop()


def test_stop_waits_for_a_concurrent_ensure_running(tmp_path: Path) -> None:
    """``stop()`` must not reap the forward another thread's ``ensure_running`` is waiting on.

    ``ensure_running`` holds the supervisor lock across its spawn *and* the wait
    for that child to claim the directory, so a ``stop()`` that ignored the lock
    would find the child, kill it, and leave the spawning thread to fail after
    its whole ownership timeout. Holding the lock here stands in for that wait.
    """
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary="/nonexistent-binary",
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )
    stopped = threading.Event()

    def _stop_then_signal() -> None:
        supervisor.stop()
        stopped.set()

    stopper = threading.Thread(target=_stop_then_signal, daemon=True)
    with supervisor._lock:
        stopper.start()
        assert not stopped.wait(timeout=0.5), "stop() ran while another caller held the supervisor lock"
    assert stopped.wait(timeout=10.0), "stop() never ran after the supervisor lock was released"
    stopper.join(timeout=10.0)


def test_stop_terminates_a_forward_that_has_only_just_claimed_the_directory(tmp_path: Path) -> None:
    """A forward that owns the directory but has not finished starting is still stoppable.

    ``ensure_running`` returns the moment the child takes the lock, which is well
    before the forward installs its SIGTERM handler -- the real one does that
    after the gateway is up. The other stop tests wait for the forward to settle
    first, so this is the only one that signals into that window.
    """
    fake_binary = _make_fake_mngr_binary(tmp_path)
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )
    info = supervisor.ensure_running()
    supervisor.stop()
    assert _wait_for_process_exit(info.pid)


def test_restart_terminates_existing_and_spawns_fresh(tmp_path: Path) -> None:
    """``restart()`` always replaces the running supervisor."""
    fake_binary = _make_fake_mngr_binary(tmp_path)
    latchkey_directory = tmp_path / f"latchkey-{uuid4().hex}"

    # Round 1: start a supervisor and let it publish its record. This
    # simulates a 'previous minds session left a supervisor running'
    # situation that a fresh minds startup will encounter.
    supervisor_old = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=latchkey_directory,
    )
    info_old = supervisor_old.ensure_running()
    _wait_for_forward_record(supervisor_old.plugin_data_dir)
    assert _wait_for_process_alive(info_old.pid)

    # Round 2: a fresh supervisor (new minds process). ``restart()``
    # must terminate the old PID and produce a new one.
    supervisor_new = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=latchkey_directory,
    )
    info_new = supervisor_new.restart()
    try:
        assert info_new.pid != info_old.pid
        assert _wait_for_process_exit(info_old.pid)
        assert _wait_for_process_alive(info_new.pid)
        new_record = _wait_for_forward_record(supervisor_new.plugin_data_dir)
        assert new_record.pid == info_new.pid
    finally:
        supervisor_new.stop()
        assert _wait_for_process_exit(info_new.pid)


def test_restart_is_a_clean_spawn_when_no_previous_supervisor(tmp_path: Path) -> None:
    """``restart()`` on a fresh latchkey directory is equivalent to ``ensure_running()``."""
    fake_binary = _make_fake_mngr_binary(tmp_path)
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )
    info = supervisor.restart()
    try:
        assert _wait_for_process_alive(info.pid)
    finally:
        supervisor.stop()
        assert _wait_for_process_exit(info.pid)


def test_get_forward_owner_returns_none_when_unstarted(tmp_path: Path) -> None:
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary="/nonexistent-binary",
        latchkey_binary="/nonexistent-binary",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )
    assert supervisor.get_forward_owner() is None


def test_a_malformed_pre_lock_record_does_not_block_the_spawn(tmp_path: Path) -> None:
    """A pre-lock record too damaged to name a forward is cleared, not left to be re-read.

    CLEANUP: delete with ``_pre_lock_migration``.

    Left behind it would be parsed and warned about on every launch, and the
    migration would never become the no-op that lets the module go.
    """
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
        assert not forward_info_path(plugin_dir).is_file()
    finally:
        supervisor.stop()
        assert _wait_for_process_exit(info.pid)


# -- extra_env propagation --------------------------------------------------


def _make_env_dumping_mngr_binary(tmp_path: Path) -> Path:
    """Build a fake ``mngr`` that records selected env vars before idling.

    Behaves like :func:`_make_fake_mngr_binary` (takes the ownership lock,
    idles until SIGTERM) and additionally dumps every env var whose name
    starts with ``MINDS_API_PROXY_TEST_`` plus the
    ``LATCHKEY_EXTENSION_MINDS_API_URL`` value to a JSON file at the
    path given in ``MINDS_API_PROXY_TEST_REPORT``. Used by
    :func:`test_extra_env_reaches_spawned_forward_subprocess` to
    verify that ``LatchkeyForwardSupervisor.extra_env`` actually
    reaches the child's ``os.environ``.
    """
    script = tmp_path / "mngr"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys\n"
        "from pathlib import Path\n"
        "from imbue.mngr_latchkey.store import acquire_forward_lock\n"
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
        'report_path_str = os.environ.get("MINDS_API_PROXY_TEST_REPORT")\n'
        "if report_path_str:\n"
        "    report_payload = {k: v for k, v in os.environ.items() "
        'if k.startswith("MINDS_API_PROXY_TEST_") or k == "LATCHKEY_EXTENSION_MINDS_API_URL"}\n'
        "    Path(report_path_str).write_text(json.dumps(report_payload))\n"
        'plugin_dir = latchkey_directory / "mngr_latchkey"\n'
        "plugin_dir.mkdir(parents=True, exist_ok=True)\n"
        "_forward_lock = acquire_forward_lock(plugin_dir)\n"
        "if _forward_lock is None:\n"
        "    sys.exit(97)\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n"
    )
    script.chmod(0o755)
    return script


def _wait_for_report_file(report_path: Path, timeout: float = 5.0) -> dict[str, str]:
    """Block until the fake mngr binary writes ``report_path``; return parsed JSON."""
    deadline = time.monotonic() + timeout
    waiter = threading.Event()
    while time.monotonic() < deadline:
        if report_path.is_file():
            return json.loads(report_path.read_text())
        waiter.wait(timeout=_POLL_INTERVAL_SECONDS)
    raise AssertionError(f"env report file never appeared at {report_path} within {timeout}s")


def test_extra_env_reaches_spawned_forward_subprocess(tmp_path: Path) -> None:
    """Values in ``extra_env`` show up in the spawned forward child's ``os.environ``.

    This is the contract that lets minds publish
    ``LATCHKEY_EXTENSION_MINDS_API_URL`` to the gateway extension on
    every supervisor restart -- if the env var did not reach the
    forward child, it would not reach the gateway, and the proxy
    extension would fall back to its 'not configured' 503.
    """
    fake_binary = _make_env_dumping_mngr_binary(tmp_path)
    report_path = tmp_path / "env_report.json"
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
        extra_env={
            "MINDS_API_PROXY_TEST_REPORT": str(report_path),
            "LATCHKEY_EXTENSION_MINDS_API_URL": "http://127.0.0.1:12345",
        },
    )
    info = supervisor.ensure_running()
    try:
        report = _wait_for_report_file(report_path)
        assert report == {
            "MINDS_API_PROXY_TEST_REPORT": str(report_path),
            "LATCHKEY_EXTENSION_MINDS_API_URL": "http://127.0.0.1:12345",
        }
    finally:
        supervisor.stop()
        assert _wait_for_process_exit(info.pid)


def test_extra_env_defaults_to_empty_mapping(tmp_path: Path) -> None:
    """A supervisor constructed without ``extra_env`` carries an empty mapping.

    Pins the default so callers that do not need extra env vars are
    not forced to spell out an explicit empty dict, and so the
    ``Mapping`` field type does not accidentally become ``None`` at
    runtime (which would crash :func:`spawn_detached_mngr_latchkey_forward`).
    """
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary="/nonexistent-binary",
        latchkey_binary="/nonexistent-binary",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )
    assert dict(supervisor.extra_env) == {}


# forward ownership and reaping


def _spawn_orphan_fake_forward(fake_binary: Path, latchkey_directory: Path) -> subprocess.Popen:
    """Spawn a detached fake ``mngr latchkey forward`` outside any supervisor.

    Stands in for a forward left running by a prior or concurrent embedder
    instance against ``latchkey_directory``. Started in its own session so a
    SIGTERM aimed at it never reaches the test's own process group.
    """
    process = subprocess.Popen(
        [str(fake_binary), "latchkey", "forward", "--latchkey-directory", str(latchkey_directory)],
        start_new_session=True,
    )
    assert _wait_for_process_alive(process.pid), "orphan forward never started"
    assert _wait_for_forward_owner(latchkey_directory, process.pid), "orphan forward never took the ownership lock"
    return process


def _wait_for_forward_owner(latchkey_directory: Path, pid: int, timeout: float = 5.0) -> bool:
    """Poll until ``pid`` is the recorded owner of ``latchkey_directory``'s forward."""
    poll_event = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_forward_owned_by(plugin_data_dir(latchkey_directory), pid):
            return True
        poll_event.wait(timeout=_POLL_INTERVAL_SECONDS)
    return False


def _terminate_orphan(process: subprocess.Popen) -> None:
    """Best-effort cleanup for an orphan spawned directly by a test."""
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def _make_idle_binary(tmp_path: Path) -> Path:
    """Build a tiny executable that idles until SIGTERM, for use as a child process."""
    binary_dir = tmp_path / f"idle-bin-{uuid4().hex}"
    binary_dir.mkdir()
    script = binary_dir / "idle"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import signal, sys\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n"
    )
    script.chmod(0o755)
    return script


def test_descendant_processes_returns_all_children_not_just_observe(tmp_path: Path) -> None:
    """``_descendant_processes`` returns every descendant, so the reaper kills the orphan's
    ``latchkey gateway`` and reverse ``ssh`` tunnels too -- not only its ``mngr observe``
    child -- when a wedged forward had to be SIGKILLed (and so never ran its teardown).

    The child here is spawned with a non-``mngr observe`` argv (gateway-shaped) to pin
    that the capture is by descendancy, not by cmdline matching.
    """
    idle_binary = _make_idle_binary(tmp_path)
    child = subprocess.Popen([str(idle_binary), "gateway"], start_new_session=True)
    try:
        deadline = time.monotonic() + 5.0
        waiter = threading.Event()
        while time.monotonic() < deadline and child.pid not in {
            p.pid for p in _descendant_processes(psutil.Process())
        }:
            waiter.wait(timeout=_POLL_INTERVAL_SECONDS)
        assert child.pid in {p.pid for p in _descendant_processes(psutil.Process())}
    finally:
        child.terminate()
        child.wait(timeout=5.0)


def _terminate_pid_if_alive(pid: int) -> None:
    """Test-teardown kill that tolerates an already-dead process."""
    try:
        psutil.Process(pid).kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def _wait_for_child_pid(data_dir: Path) -> int:
    """Block until the fake forward has recorded the child it spawned.

    The fake takes the ownership lock before spawning that child, and
    ``ensure_running`` returns as soon as the directory is owned -- so the file
    does not exist yet at that point. Fails the test on timeout.
    """
    path = data_dir / "child_pid.txt"
    deadline = time.monotonic() + _FORWARD_RECORD_POLL_TIMEOUT
    waiter = threading.Event()
    while time.monotonic() < deadline:
        if path.is_file():
            recorded = path.read_text().strip()
            if recorded:
                return int(recorded)
        waiter.wait(timeout=_FORWARD_RECORD_POLL_INTERVAL)
    raise AssertionError(f"fake forward never recorded its child at {path}")


def _make_fake_forward_spawning_child_binary(tmp_path: Path) -> Path:
    """A fake ``mngr`` whose ``latchkey forward`` spawns a long-lived child and records its PID.

    The child stands in for the forward's owned subprocesses (``mngr observe`` /
    ``latchkey gateway`` / reverse ``ssh`` tunnels). The fake forward exits on
    SIGTERM *without* killing the child, so the child survives unless the reaper
    terminates it directly -- which is exactly the wedged-forward behavior under
    test. The child PID is written to ``<plugin_dir>/child_pid.txt``.
    """
    script = tmp_path / "mngr"
    script.write_text(
        f"#!{sys.executable}\n"
        "import signal, subprocess, sys\n"
        "from pathlib import Path\n"
        "from imbue.mngr_latchkey.store import acquire_forward_lock\n"
        'if sys.argv[1:3] != ["latchkey", "forward"]:\n'
        "    sys.exit(99)\n"
        "latchkey_directory = None\n"
        "args = sys.argv[3:]\n"
        "for i, arg in enumerate(args):\n"
        '    if arg == "--latchkey-directory" and i + 1 < len(args):\n'
        "        latchkey_directory = Path(args[i + 1])\n"
        "        break\n"
        "if latchkey_directory is None:\n"
        "    sys.exit(98)\n"
        'plugin = latchkey_directory / "mngr_latchkey"\n'
        "plugin.mkdir(parents=True, exist_ok=True)\n"
        # Own the directory the way the real forward does, so the reaper can find it.
        "_forward_lock = acquire_forward_lock(plugin)\n"
        "if _forward_lock is None:\n"
        "    sys.exit(97)\n"
        'child = subprocess.Popen(["sleep", "600"])\n'
        '(plugin / "child_pid.txt").write_text(str(child.pid))\n'
        # Exit on SIGTERM WITHOUT tearing down the child, so only the reaper's
        # explicit descendant-termination can kill it.
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n"
    )
    script.chmod(0o755)
    return script


def test_restart_reaps_orphan_forwards_children_too(tmp_path: Path) -> None:
    """Reaping an orphan forward also terminates its descendant subprocesses.

    The orphan fake spawns a long-lived child and exits on SIGTERM without killing
    it, so the child survives only if the reaper terminates it directly -- mirroring
    a wedged forward whose observe/gateway/tunnel children would otherwise be left
    orphaned.
    """
    fake_binary = _make_fake_forward_spawning_child_binary(tmp_path)
    latchkey_directory = tmp_path / f"latchkey-{uuid4().hex}"
    orphan = _spawn_orphan_fake_forward(fake_binary, latchkey_directory)
    supervisor: LatchkeyForwardSupervisor | None = None
    info: LatchkeyForwardOwner | None = None
    child_pid: int | None = None
    try:
        data_dir = plugin_data_dir(latchkey_directory)
        _wait_for_forward_record(data_dir)
        child_pid = _wait_for_child_pid(data_dir)
        assert psutil.pid_exists(child_pid)
        supervisor = LatchkeyForwardSupervisor(
            mngr_binary=str(fake_binary),
            latchkey_binary="/usr/bin/latchkey-unused",
            latchkey_directory=latchkey_directory,
        )
        # ``restart`` is the verb that replaces a live owner; ``ensure_running`` adopts one.
        info = supervisor.restart()
        assert _wait_for_process_exit(orphan.pid), "the orphan forward was not reaped"
        assert _wait_for_process_exit(child_pid), "the orphan forward's child was not reaped"
    finally:
        if supervisor is not None and info is not None:
            supervisor.stop()
            _wait_for_process_exit(info.pid)
        _terminate_orphan(orphan)
        if child_pid is not None:
            _terminate_pid_if_alive(child_pid)


def test_stop_terminates_descendants_of_wedged_supervisor(tmp_path: Path) -> None:
    """``stop()`` also terminates the supervisor's descendant subprocesses.

    A healthy supervisor tears its own descendants (``mngr observe``, the
    ``latchkey gateway``, reverse tunnels) down in its SIGTERM handler -- but a
    wedged one that has to be SIGKILLed (or, as here, one that exits without
    running its teardown) never does, and the detached gateway then outlives
    every session. ``stop()`` must capture the descendants before signalling
    and terminate them after.
    """
    fake_binary = _make_fake_forward_spawning_child_binary(tmp_path)
    latchkey_directory = tmp_path / f"latchkey-{uuid4().hex}"
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=latchkey_directory,
    )
    child_pid: int | None = None
    info: LatchkeyForwardOwner | None = None
    try:
        info = supervisor.ensure_running()
        data_dir = plugin_data_dir(latchkey_directory)
        _wait_for_forward_record(data_dir)
        child_pid = _wait_for_child_pid(data_dir)
        assert psutil.pid_exists(child_pid)

        supervisor.stop()
        assert _wait_for_process_exit(info.pid)
        assert _wait_for_process_exit(child_pid), "the supervisor's child was not reaped by stop()"
    finally:
        if info is not None:
            _terminate_pid_if_alive(info.pid)
        if child_pid is not None:
            _terminate_pid_if_alive(child_pid)


def test_stop_terminates_descendants_via_on_disk_record(tmp_path: Path) -> None:
    """``stop()`` reaps descendants from a supervisor object that never spawned it.

    A new minds session stops the previous session's forward, having no cached
    pid of its own; an orphaned gateway must not survive that path either.
    """
    fake_binary = _make_fake_forward_spawning_child_binary(tmp_path)
    latchkey_directory = tmp_path / f"latchkey-{uuid4().hex}"
    old_supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=latchkey_directory,
    )
    child_pid: int | None = None
    info: LatchkeyForwardOwner | None = None
    try:
        info = old_supervisor.ensure_running()
        data_dir = plugin_data_dir(latchkey_directory)
        _wait_for_forward_record(data_dir)
        child_pid = _wait_for_child_pid(data_dir)
        assert psutil.pid_exists(child_pid)

        # A fresh supervisor object (a new minds session) only has the record.
        fresh_supervisor = LatchkeyForwardSupervisor(
            mngr_binary=str(fake_binary),
            latchkey_binary="/usr/bin/latchkey-unused",
            latchkey_directory=latchkey_directory,
        )
        fresh_supervisor.stop()
        assert _wait_for_process_exit(info.pid)
        assert _wait_for_process_exit(child_pid), "the supervisor's child was not reaped by stop()"
    finally:
        if info is not None:
            _terminate_pid_if_alive(info.pid)
        if child_pid is not None:
            _terminate_pid_if_alive(child_pid)


@pytest.mark.flaky
def test_owning_forward_process_names_the_forward_holding_the_directory(tmp_path: Path) -> None:
    """Ownership is answered from the directory alone, and is scoped to that directory.

    This is the check the reaper applies before signalling anything, so a
    directory mismatch or a dead pid must never come back as an owner.

    Marked flaky: it spawns and reaps a real subprocess, and its process-state
    polls have been seen to run out on a heavily loaded machine.
    """
    fake_binary = _make_fake_mngr_binary(tmp_path)
    own_directory = tmp_path / f"own-{uuid4().hex}"
    other_directory = tmp_path / f"other-{uuid4().hex}"
    forward = _spawn_orphan_fake_forward(fake_binary, own_directory)
    try:
        assert is_forward_owned_by(plugin_data_dir(own_directory), forward.pid)
        assert owning_forward_process(plugin_data_dir(other_directory)) is None
    finally:
        _terminate_orphan(forward)
    # The kernel drops the lock when the owner dies, so the recorded owner --
    # still on disk -- must read as gone rather than as live.
    assert _wait_for_process_exit(forward.pid)
    assert owning_forward_process(plugin_data_dir(own_directory)) is None


def _make_pre_lock_fake_mngr_binary(tmp_path: Path) -> Path:
    """Build a fake ``mngr`` that behaves the way one from before the lock did.

    CLEANUP: delete with ``_pre_lock_migration``.

    It publishes a forward record and idles, and takes no ownership lock,
    because the build it stands in for had none to take.
    """
    binary_dir = tmp_path / f"pre-lock-{uuid4().hex}"
    binary_dir.mkdir()
    script = binary_dir / "mngr"
    script.write_text(
        f"#!{sys.executable}\n"
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
        'record_path = latchkey_directory / "mngr_latchkey" / "latchkey_forward.json"\n'
        "record_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "record_path.write_text(json.dumps({\n"
        '    "pid": os.getpid(),\n'
        '    "started_at": datetime.now(timezone.utc).isoformat(),\n'
        '    "gateway_port": None,\n'
        "}))\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.pause()\n"
    )
    script.chmod(0o755)
    return script


def test_restart_replaces_a_forward_that_predates_the_ownership_lock(tmp_path: Path) -> None:
    """The upgrade path: the first launch after an update replaces the old forward.

    CLEANUP: delete with ``_pre_lock_migration``.

    A forward from a build without the lock holds none, so nothing about the
    lock can see it -- only the record it wrote. If that record stopped being
    enough, the replacement would take the free lock and run *beside* the old
    forward, putting two discovery producers on one events file.
    """
    pre_lock_binary = _make_pre_lock_fake_mngr_binary(tmp_path)
    current_binary = _make_fake_mngr_binary(tmp_path)
    latchkey_directory = tmp_path / f"latchkey-{uuid4().hex}"
    data_dir = plugin_data_dir(latchkey_directory)

    pre_lock = subprocess.Popen(
        [str(pre_lock_binary), "latchkey", "forward", "--latchkey-directory", str(latchkey_directory)],
        start_new_session=True,
    )
    supervisor: LatchkeyForwardSupervisor | None = None
    info: LatchkeyForwardOwner | None = None
    try:
        _wait_for_legacy_forward_record(data_dir)
        # It holds no lock, exactly as a pre-lock forward does not.
        assert owning_forward_process(data_dir) is None

        supervisor = LatchkeyForwardSupervisor(
            mngr_binary=str(current_binary),
            latchkey_binary="/usr/bin/latchkey-unused",
            latchkey_directory=latchkey_directory,
        )
        info = supervisor.restart()

        assert _wait_for_process_exit(pre_lock.pid), "the pre-lock forward outlived the update"
        assert info.pid != pre_lock.pid
        assert _wait_for_forward_owner(latchkey_directory, info.pid), "the replacement never took the lock"
    finally:
        if supervisor is not None and info is not None:
            supervisor.stop()
            _wait_for_process_exit(info.pid)
        _terminate_orphan(pre_lock)


def test_ensure_running_refuses_a_child_that_never_took_the_directory(tmp_path: Path) -> None:
    """A spawn that does not end in ownership is terminated, and raises.

    Leaving it would orphan a process nothing else knows about: it never
    recorded itself, so no later call can find it.
    """
    # An idle binary starts and never claims the directory.
    fake_binary = _make_idle_binary(tmp_path)
    latchkey_directory = tmp_path / f"latchkey-{uuid4().hex}"
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=latchkey_directory,
        spawn_ownership_timeout_seconds=2.0,
    )
    before = {p.pid for p in psutil.Process().children(recursive=True)}
    with pytest.raises(LatchkeyError, match="did not take ownership"):
        supervisor.ensure_running()
    leaked = {p.pid for p in psutil.Process().children(recursive=True) if p.is_running()} - before
    assert not leaked, f"a child that never claimed the directory was left running: {leaked}"


def _make_racing_fake_mngr_binary(tmp_path: Path) -> Path:
    """A fake ``mngr`` whose ``latchkey forward`` loses the directory to another process.

    It hands the claim to a detached process and exits refusing the directory,
    which is what a real forward does when another one owns it -- leaving the
    directory owned by a pid that is not the one the spawn returned.
    """
    binary_dir = tmp_path / f"racing-{uuid4().hex}"
    binary_dir.mkdir()
    script = binary_dir / "mngr"
    script.write_text(
        f"#!{sys.executable}\n"
        "import signal, subprocess, sys\n"
        "from pathlib import Path\n"
        "from imbue.mngr_latchkey.store import acquire_forward_lock\n"
        "args = sys.argv[1:]\n"
        'if args[:1] == ["__own__"]:\n'
        '    plugin_dir = Path(args[1]) / "mngr_latchkey"\n'
        "    plugin_dir.mkdir(parents=True, exist_ok=True)\n"
        "    _forward_lock = acquire_forward_lock(plugin_dir)\n"
        "    if _forward_lock is None:\n"
        "        sys.exit(97)\n"
        "    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "    signal.pause()\n"
        'if args[:2] != ["latchkey", "forward"]:\n'
        "    sys.exit(99)\n"
        "latchkey_directory = None\n"
        "for i, arg in enumerate(args):\n"
        '    if arg == "--latchkey-directory" and i + 1 < len(args):\n'
        "        latchkey_directory = args[i + 1]\n"
        "        break\n"
        "if latchkey_directory is None:\n"
        "    sys.exit(98)\n"
        'subprocess.Popen([sys.executable, __file__, "__own__", latchkey_directory], start_new_session=True)\n'
        "sys.exit(97)\n"
    )
    script.chmod(0o755)
    return script


def test_ensure_running_adopts_the_forward_that_won_a_spawn_race(tmp_path: Path) -> None:
    """A child that loses the directory to another forward is dropped for the winner.

    The adopt check and the spawn are not atomic across processes, so the child
    can find the directory already taken and refuse it within a second. Waiting
    for *this* child would then spend the whole ownership timeout and fail a
    caller whose latchkey directory has a healthy forward on it.
    """
    fake_binary = _make_racing_fake_mngr_binary(tmp_path)
    latchkey_directory = tmp_path / f"latchkey-{uuid4().hex}"
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary=str(fake_binary),
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=latchkey_directory,
        spawn_ownership_timeout_seconds=10.0,
    )
    started_at = time.monotonic()
    try:
        info = supervisor.ensure_running()
        assert time.monotonic() - started_at < supervisor.spawn_ownership_timeout_seconds, (
            "the spawn race was waited out rather than resolved"
        )
        assert is_forward_owned_by(plugin_data_dir(latchkey_directory), info.pid)
    finally:
        # The winner is detached and in its own session, so only ``stop`` -- which
        # finds it through the lock rather than through the returned record --
        # reaches it when ``ensure_running`` raised instead of naming it.
        supervisor.stop()
    assert _wait_for_process_exit(info.pid)


def test_owning_forward_process_reads_no_owner_when_the_stamp_never_lands(tmp_path: Path) -> None:
    """A holder that dies before publishing reads as no owner.

    A departed owner's record outlives it. If the gap before the stamp exposed
    the old pid, a probe could hand the reaper a process that is dead -- or a
    stranger that recycled its pid. Taking the lock clears the record, so the
    gap shows nothing instead of something wrong.
    """
    data_dir = plugin_data_dir(tmp_path)
    data_dir.mkdir(parents=True)
    owner_path = forward_owner_path(data_dir)
    # A departed owner's record, left exactly as its death leaves it.
    owner_path.write_text(LatchkeyForwardOwner(pid=os.getpid()).model_dump_json())
    lock = acquire_forward_lock(data_dir)
    assert lock is not None
    try:
        # Reproduce a holder that took the lock and then never stamped it.
        owner_path.unlink()
        assert owning_forward_process(data_dir) is None
    finally:
        lock.release()


def test_owning_forward_process_waits_for_a_holder_still_publishing(tmp_path: Path) -> None:
    """A held lock whose record has not landed yet waits for it, rather than reporting no owner.

    Reading once would call the directory unowned while a healthy forward holds
    it, and the caller would spawn a second forward that this one then refuses
    -- failing the caller outright instead of adopting the forward it has.
    """
    data_dir = plugin_data_dir(tmp_path)
    data_dir.mkdir(parents=True)
    owner_path = forward_owner_path(data_dir)
    lock = acquire_forward_lock(data_dir)
    assert lock is not None
    published = LatchkeyForwardOwner(pid=os.getpid()).model_dump_json()
    owner_path.unlink()
    publish_delay = 0.2
    publisher = threading.Timer(publish_delay, lambda: owner_path.write_text(published))
    publisher.start()
    try:
        started_at = time.monotonic()
        forward_process = owning_forward_process(data_dir)
        waited = time.monotonic() - started_at
        assert forward_process is not None, "a holder mid-publish must not read as an unowned directory"
        assert forward_process.pid == os.getpid()
        # Reading the record once would have returned before the stamp landed.
        assert waited >= publish_delay, f"probe returned after {waited:.3f}s without waiting for the stamp"
    finally:
        publisher.cancel()
        lock.release()


def test_owning_forward_process_ignores_an_owner_record_nobody_backs(tmp_path: Path) -> None:
    """A record left behind by a departed owner reads as unowned.

    Liveness is whether the lock is held, never a value stored in the record.
    The planted pid is a live forward, so nothing but the lock can still answer
    "nobody" -- and that forward owns a *different* directory, so a probe that
    trusted the record would hand the reaper a sibling profile's supervisor to
    signal.
    """
    fake_binary = _make_fake_mngr_binary(tmp_path)
    owned_directory = tmp_path / f"owned-{uuid4().hex}"
    forward = _spawn_orphan_fake_forward(fake_binary, owned_directory)
    try:
        unowned_data_dir = plugin_data_dir(tmp_path / f"unowned-{uuid4().hex}")
        # Leave the directory exactly as a departed owner does: a real lock file
        # nobody holds, and the record that owner wrote still beside it.
        released_lock = acquire_forward_lock(unowned_data_dir)
        assert released_lock is not None
        released_lock.release()
        forward_owner_path(unowned_data_dir).write_text(LatchkeyForwardOwner(pid=forward.pid).model_dump_json())
        assert forward_lock_path(unowned_data_dir).is_file()
        assert owning_forward_process(unowned_data_dir) is None
    finally:
        _terminate_orphan(forward)


def test_owning_forward_process_survives_a_directory_containing_a_space(tmp_path: Path) -> None:
    """A latchkey directory with a space is recognised as its forward's own.

    An embedder may place one under ``~/Library/Application Support/<App>/``, and
    a home directory such as ``/Users/Jane Doe`` produces one unprompted.
    """
    fake_binary = _make_fake_mngr_binary(tmp_path)
    spaced_directory = tmp_path / "Application Support" / f"Example App {uuid4().hex}" / "latchkey"
    forward = _spawn_orphan_fake_forward(fake_binary, spaced_directory)
    try:
        assert is_forward_owned_by(plugin_data_dir(spaced_directory), forward.pid)
    finally:
        _terminate_orphan(forward)


def test_a_second_forward_cannot_start_for_the_same_directory(tmp_path: Path) -> None:
    """The ownership lock makes a duplicate forward impossible, not merely reapable.

    Two forwards would put two ``mngr observe`` producers on the shared events
    file.
    """
    fake_binary = _make_fake_mngr_binary(tmp_path)
    latchkey_directory = tmp_path / f"latchkey-{uuid4().hex}"
    first = _spawn_orphan_fake_forward(fake_binary, latchkey_directory)
    try:
        second = subprocess.run(
            [str(fake_binary), "latchkey", "forward", "--latchkey-directory", str(latchkey_directory)],
            capture_output=True,
            timeout=10.0,
        )
        assert second.returncode == 97, f"second forward should have refused to start: {second!r}"
        assert is_forward_owned_by(plugin_data_dir(latchkey_directory), first.pid)
    finally:
        _terminate_orphan(first)


@pytest.mark.parametrize(
    "directory_template",
    (
        pytest.param("latchkey-{}", id="plain"),
        pytest.param("Application Support/Example App {}/latchkey", id="spaced"),
    ),
)
def test_ensure_running_adopts_a_forward_that_already_owns_the_directory(
    tmp_path: Path, directory_template: str
) -> None:
    """A live owner is adopted, never duplicated.

    Two forwards on one directory would put two ``mngr observe`` producers on
    the shared events file, which is the failure the ownership lock exists to
    make impossible. Parameterised over a directory containing a space, which
    is what the whole ownership change exists to keep working.
    """
    fake_binary = _make_fake_mngr_binary(tmp_path)
    latchkey_directory = tmp_path / directory_template.format(uuid4().hex)
    orphan = _spawn_orphan_fake_forward(fake_binary, latchkey_directory)
    try:
        supervisor = LatchkeyForwardSupervisor(
            mngr_binary=str(fake_binary),
            latchkey_binary="/usr/bin/latchkey-unused",
            latchkey_directory=latchkey_directory,
        )
        info = supervisor.ensure_running()
        assert info.pid == orphan.pid, "the forward already owning the directory should be adopted"
        assert psutil.pid_exists(orphan.pid), "adoption must leave the owner running"
    finally:
        _terminate_orphan(orphan)


def test_ensure_running_adopts_the_recorded_owner(tmp_path: Path) -> None:
    """When the record points at the forward that owns the directory, it is adopted."""
    fake_binary = _make_fake_mngr_binary(tmp_path)
    latchkey_directory = tmp_path / f"latchkey-{uuid4().hex}"
    data_dir = plugin_data_dir(latchkey_directory)
    owner = _spawn_orphan_fake_forward(fake_binary, latchkey_directory)
    try:
        _wait_for_forward_ready(data_dir, owner.pid)
        supervisor = LatchkeyForwardSupervisor(
            mngr_binary=str(fake_binary),
            latchkey_binary="/usr/bin/latchkey-unused",
            latchkey_directory=latchkey_directory,
        )
        info = supervisor.ensure_running()
        assert info.pid == owner.pid, "the recorded owner should be adopted, not respawned"
        assert psutil.pid_exists(owner.pid), "the adopted forward must stay alive"
        assert is_forward_owned_by(data_dir, owner.pid), "adoption must not disturb ownership"
    finally:
        _terminate_orphan(owner)


def test_ensure_running_does_not_reap_forward_for_a_different_directory(tmp_path: Path) -> None:
    """A forward bound to a *different* latchkey directory is never signalled.

    The safety boundary: a ``.minds`` supervisor must not reap a sibling
    profile's (``.minds-staging`` / ``.minds-dev``) forward.
    """
    fake_binary = _make_fake_mngr_binary(tmp_path)
    other_directory = tmp_path / f"other-{uuid4().hex}"
    own_directory = tmp_path / f"own-{uuid4().hex}"
    other = _spawn_orphan_fake_forward(fake_binary, other_directory)
    supervisor: LatchkeyForwardSupervisor | None = None
    info: LatchkeyForwardOwner | None = None
    try:
        supervisor = LatchkeyForwardSupervisor(
            mngr_binary=str(fake_binary),
            latchkey_binary="/usr/bin/latchkey-unused",
            latchkey_directory=own_directory,
        )
        info = supervisor.ensure_running()
        # ``ensure_running`` reaps synchronously, so by here any erroneous
        # signal would already have been sent: the other forward must be intact.
        assert psutil.pid_exists(other.pid)
        assert not _wait_for_process_exit(other.pid, timeout=1.0)
    finally:
        if supervisor is not None and info is not None:
            supervisor.stop()
            _wait_for_process_exit(info.pid)
        _terminate_orphan(other)


def _wait_for_sighup_sentinel(plugin_dir: Path, pid: int, timeout: float = 5.0) -> bool:
    """Poll until the fake forward's SIGHUP-delivery sentinel for ``pid`` appears."""
    sentinel = plugin_dir / f"sighup_received_{pid}.txt"
    poll_event = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sentinel.exists():
            return True
        poll_event.wait(timeout=_POLL_INTERVAL_SECONDS)
    return False


def test_bounce_skips_sighup_while_forward_is_still_starting(tmp_path: Path) -> None:
    """A live forward whose record carries no gateway port yet is never signalled.

    The port is stamped only once startup completes; before that a SIGHUP can
    land ahead of the real forward's handler installation, where the default
    disposition kills it. The fake forward's SIGHUP handler writes a delivery
    sentinel, so an absent sentinel after the settle window proves the signal
    was skipped.
    """
    fake_binary = _make_fake_mngr_binary(tmp_path)
    latchkey_directory = tmp_path / f"latchkey-{uuid4().hex}"
    data_dir = plugin_data_dir(latchkey_directory)
    fake = _spawn_orphan_fake_forward(fake_binary, latchkey_directory)
    try:
        _wait_for_forward_ready(data_dir, fake.pid)
        assert _wait_for_process_alive(fake.pid)
        supervisor = LatchkeyForwardSupervisor(
            mngr_binary=str(fake_binary),
            latchkey_binary="/usr/bin/latchkey-unused",
            latchkey_directory=latchkey_directory,
        )

        # The fake's own record has ``gateway_port: None`` (still starting).
        supervisor.bounce()

        assert not _wait_for_sighup_sentinel(data_dir, fake.pid, timeout=1.0), (
            "a still-starting forward must not be signalled"
        )
        assert psutil.pid_exists(fake.pid), "a still-starting forward must stay alive"
        assert is_forward_owned_by(data_dir, fake.pid), "bounce must not replace a still-starting forward"
    finally:
        _terminate_orphan(fake)


def test_bounce_sighups_forward_once_gateway_port_is_stamped(tmp_path: Path) -> None:
    """Once the record carries a gateway port, ``bounce()`` delivers the SIGHUP.

    The fake forward's SIGHUP handler writes a delivery sentinel -- the
    counterpart to the still-starting skip above, proving the readiness guard
    does not suppress bounces for fully-started supervisors.
    """
    fake_binary = _make_fake_mngr_binary(tmp_path)
    latchkey_directory = tmp_path / f"latchkey-{uuid4().hex}"
    data_dir = plugin_data_dir(latchkey_directory)
    fake = _spawn_orphan_fake_forward(fake_binary, latchkey_directory)
    try:
        _wait_for_forward_ready(data_dir, fake.pid)
        assert _wait_for_process_alive(fake.pid)
        update_forward_owner_gateway_port(data_dir, 45999)
        supervisor = LatchkeyForwardSupervisor(
            mngr_binary=str(fake_binary),
            latchkey_binary="/usr/bin/latchkey-unused",
            latchkey_directory=latchkey_directory,
        )

        supervisor.bounce()

        assert _wait_for_sighup_sentinel(data_dir, fake.pid), "a ready forward must receive the SIGHUP bounce"
        assert psutil.pid_exists(fake.pid), "the bounce must not kill a forward with a SIGHUP handler installed"
    finally:
        _terminate_orphan(fake)
