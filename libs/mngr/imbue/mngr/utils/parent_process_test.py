import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.parent_process import _PARENT_POLL_INTERVAL_SECONDS
from imbue.mngr.utils.parent_process import _read_grandparent_pid
from imbue.mngr.utils.parent_process import _read_ppid_via_ps
from imbue.mngr.utils.parent_process import start_grandparent_death_watcher
from imbue.mngr.utils.parent_process import start_parent_death_watcher
from imbue.mngr.utils.polling import wait_for


def test_start_parent_death_watcher_starts_thread_in_concurrency_group() -> None:
    """Verify the watcher thread is started and is alive."""
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        start_parent_death_watcher(cg)
        threads = [t for t in cg._threads if t.thread.name == "parent-death-watcher"]
        assert len(threads) == 1
        assert threads[0].thread.is_alive()


def test_parent_death_watcher_does_not_fire_when_parent_alive() -> None:
    """Verify the watcher thread stays alive through a poll cycle when the parent is still alive."""
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        start_parent_death_watcher(cg)
        threads = [t for t in cg._threads if t.thread.name == "parent-death-watcher"]
        assert len(threads) == 1
        watcher_thread = threads[0].thread

        # Poll until the watcher has had time for at least one full poll cycle.
        # If the watcher incorrectly fired, the thread would exit after detecting
        # a (false) parent death.
        deadline = threading.Event()
        deadline.wait(timeout=_PARENT_POLL_INTERVAL_SECONDS + 1.0)
        assert watcher_thread.is_alive(), "Watcher thread exited unexpectedly during poll cycle"


def test_read_grandparent_pid_returns_alive_grandparent() -> None:
    """The helper should return a positive, signalable PID when a grandparent exists.

    Pytest under xdist runs each test inside a worker that has a real parent
    and grandparent, so locally this always resolves. Some offload sandboxes
    run pytest directly under PID 1, leaving no grandparent; in that case the
    helper correctly returns ``None`` and the test skips.
    """
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        grandparent_pid = _read_grandparent_pid(cg)
    if grandparent_pid is None:
        pytest.skip("No resolvable grandparent in this process tree (e.g. offload sandbox)")
    assert grandparent_pid > 1
    os.kill(grandparent_pid, 0)


def test_read_ppid_via_ps_returns_parent_pid_of_a_child_process() -> None:
    """The ``/proc``-free ppid resolver must report a child's real parent PID.

    ``ps -o ppid=`` is the cross-platform path that lets the grandparent-death
    watcher arm on macOS, which has no ``/proc`` (MIND-103). We spawn a real
    child whose parent is this test process, so resolving the child's parent
    PID must return our own PID. This runs identically on Linux and macOS, so
    it fails on the pre-fix code (the resolver does not exist) and passes once
    the fallback is added.
    """
    child = subprocess.Popen(["sleep", "51763"])
    try:
        with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
            resolved_ppid = _read_ppid_via_ps(child.pid, cg)
    finally:
        child.terminate()
        child.wait()
    assert resolved_ppid == os.getpid()


def test_start_grandparent_death_watcher_starts_thread_when_resolvable() -> None:
    """When a grandparent exists, the watcher thread is started and stays alive."""
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        start_grandparent_death_watcher(cg)
        threads = [t for t in cg._threads if t.thread.name == "grandparent-death-watcher"]
        # If the test runner has no resolvable grandparent (very unusual), the
        # watcher is a no-op; both shapes are valid.
        if _read_grandparent_pid(cg) is None:
            assert threads == []
            return
        assert len(threads) == 1
        watcher_thread = threads[0].thread
        deadline = threading.Event()
        deadline.wait(timeout=_PARENT_POLL_INTERVAL_SECONDS + 1.0)
        assert watcher_thread.is_alive(), "Grandparent watcher exited unexpectedly during poll cycle"


_ORPHAN_WATCHER_SCRIPT = """
import os
import threading
from pathlib import Path

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.parent_process import start_parent_death_watcher

with ConcurrencyGroup(name="orphan-watcher-probe") as concurrency_group:
    Path({pid_file!r}).write_text(str(os.getpid()))
    start_parent_death_watcher(concurrency_group)
    # Without the watcher this process would idle here for a long time.
    threading.Event().wait(timeout=60.0)
"""


def test_parent_death_watcher_exits_at_arm_time_when_parent_is_already_gone(tmp_path: Path) -> None:
    """A child whose parent died before the watcher armed must still exit.

    The intermediate ``sh`` backgrounds the Python child and exits immediately, so by the time
    the child has imported enough to arm its watcher it has already been reparented to init.
    A watcher that merely recorded the parent PID at arm time would record init and never fire.
    """
    pid_file = tmp_path / "orphan.pid"
    script = _ORPHAN_WATCHER_SCRIPT.format(pid_file=str(pid_file))
    subprocess.run(
        ["sh", "-c", f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} &"],
        check=True,
        timeout=30.0,
    )
    wait_for(pid_file.exists, timeout=20.0, poll_interval=0.05)
    orphan_pid = int(pid_file.read_text())

    def is_orphan_gone() -> bool:
        try:
            os.kill(orphan_pid, 0)
        except ProcessLookupError:
            return True
        return False

    try:
        wait_for(is_orphan_gone, timeout=15.0, poll_interval=0.1)
    finally:
        if not is_orphan_gone():
            os.kill(orphan_pid, 9)
