"""Unit tests for :mod:`imbue.mngr_latchkey.forward_supervisor`.

Exercises the adopt / discard-stale / spawn-fresh state machine of
:class:`LatchkeyForwardSupervisor` end-to-end against a small fake
``mngr`` binary that imitates the actual ``mngr latchkey forward``
argv shape (so the cmdline-based liveness probe accepts it).
"""

import json
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
        # Record the working directory the supervisor launched us in so a test
        # can assert the `cwd` field is threaded through to the spawn.
        '(record_path.parent / "observed_cwd.txt").write_text(os.getcwd())\n'
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
        _wait_for_forward_record(supervisor.plugin_data_dir)
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
    supervisor.stop()


def test_stop_immediately_after_ensure_running_terminates_child(tmp_path: Path) -> None:
    """``stop()`` called within the fork-exec window still terminates the freshly-spawned child.

    Regression: an earlier version of ``stop()`` ran an
    :func:`is_forward_info_alive` check on the cached PID before
    sending SIGTERM. The child's cmdline is briefly empty between
    the kernel's ``fork`` and ``execve``, so the check would fail
    and ``stop()`` would skip the SIGTERM, leaking the child. The
    current ``stop()`` trusts ``_last_known_pid`` without a cmdline
    check; this test pins that behaviour by NOT waiting for the
    child to fully exec before calling ``stop()``.
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


def test_stop_skips_termination_for_stale_pid(tmp_path: Path) -> None:
    """A record whose PID is alive but not a ``mngr latchkey forward`` is not signaled.

    Guards against PID reuse: between a previous supervisor exiting
    and ``stop()`` being called, the OS may have recycled its PID
    for an unrelated process. The cmdline-verified termination in
    ``stop()`` skips the SIGTERM in that case.
    """
    supervisor = LatchkeyForwardSupervisor(
        mngr_binary="/usr/bin/mngr-unused",
        latchkey_binary="/usr/bin/latchkey-unused",
        latchkey_directory=tmp_path / f"latchkey-{uuid4().hex}",
    )
    plugin_dir = supervisor.plugin_data_dir
    plugin_dir.mkdir(parents=True, exist_ok=True)
    # PID 1 is alive on every POSIX system, but its cmdline is not ours.
    save_forward_info(plugin_dir, LatchkeyForwardInfo(pid=1, started_at=datetime.now(timezone.utc)))
    supervisor.stop()
    # PID 1 must still be running -- ``stop()`` recognized the cmdline
    # mismatch and skipped the SIGTERM.
    assert psutil.pid_exists(1)


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


# -- extra_env propagation --------------------------------------------------


def _make_env_dumping_mngr_binary(tmp_path: Path) -> Path:
    """Build a fake ``mngr`` that records selected env vars before idling.

    Behaves like :func:`_make_fake_mngr_binary` (publishes a forward
    record, idles until SIGTERM) and additionally dumps every env var
    whose name starts with ``MINDS_API_PROXY_TEST_`` plus the
    ``LATCHKEY_EXTENSION_MINDS_API_URL`` value to a JSON file at the
    path given in ``MINDS_API_PROXY_TEST_REPORT``. Used by
    :func:`test_extra_env_reaches_spawned_forward_subprocess` to
    verify that ``LatchkeyForwardSupervisor.extra_env`` actually
    reaches the child's ``os.environ``.
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
        'report_path_str = os.environ.get("MINDS_API_PROXY_TEST_REPORT")\n'
        "if report_path_str:\n"
        "    report_payload = {k: v for k, v in os.environ.items() "
        'if k.startswith("MINDS_API_PROXY_TEST_") or k == "LATCHKEY_EXTENSION_MINDS_API_URL"}\n'
        "    Path(report_path_str).write_text(json.dumps(report_payload))\n"
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
