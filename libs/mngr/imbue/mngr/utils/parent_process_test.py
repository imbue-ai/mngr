import os
import subprocess
import sys
import threading
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.utils.parent_process import _PARENT_POLL_INTERVAL_SECONDS
from imbue.mngr.utils.parent_process import _read_grandparent_pid
from imbue.mngr.utils.parent_process import start_grandparent_death_watcher
from imbue.mngr.utils.parent_process import start_parent_death_watcher
from imbue.mngr.utils.polling import wait_for

# Inline program for the *watched child* (process C). It records its current
# parent, shrinks the watcher's poll interval so the test runs in well under a
# second, starts the real parent-death watcher in a real ConcurrencyGroup, then
# blocks forever. A SIGTERM handler writes a sentinel file (proving the SIGTERM
# came from the watcher, not from being killed by the test) and exits cleanly.
# It avoids ``time.sleep``/``setattr`` so the source-text ratchets stay happy.
_WATCHED_CHILD_PROGRAM = """
import os
import signal
import threading

import imbue.mngr.utils.parent_process as pp
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup

ready_path = os.environ["WATCHER_READY_PATH"]
sigterm_path = os.environ["WATCHER_SIGTERM_PATH"]
done = threading.Event()


def _on_sigterm(signum, frame):
    with open(sigterm_path, "w") as handle:
        handle.write(str(os.getppid()))
    done.set()


signal.signal(signal.SIGTERM, _on_sigterm)
# Poll fast so a real parent death is detected promptly.
pp._PARENT_POLL_INTERVAL_SECONDS = 0.05

cg = ConcurrencyGroup(name="watched-child")
cg.__enter__()
pp.start_parent_death_watcher(cg)
with open(ready_path, "w") as handle:
    handle.write(str(os.getpid()))
done.wait(timeout=30.0)
"""

# Inline program for the *intermediate parent* (process P). It spawns the
# watched child C (so that C's parent is P, not the test process), records C's
# PID to a file for the test to capture, then blocks until it is itself killed
# -- at which point C is reparented and its watcher must fire.
_INTERMEDIATE_PARENT_PROGRAM = """
import os
import subprocess
import sys
import threading

child = subprocess.Popen([sys.executable, "-c", os.environ["WATCHED_CHILD_PROGRAM"]])
with open(os.environ["CHILD_PID_PATH"], "w") as handle:
    handle.write(str(child.pid))
threading.Event().wait(timeout=30.0)
"""


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
    grandparent_pid = _read_grandparent_pid()
    if grandparent_pid is None:
        pytest.skip("No resolvable grandparent in this process tree (e.g. offload sandbox)")
    assert grandparent_pid > 1
    os.kill(grandparent_pid, 0)


def test_start_grandparent_death_watcher_starts_thread_when_resolvable() -> None:
    """When a grandparent exists, the watcher thread is started and stays alive."""
    with ConcurrencyGroup(name=f"test-{uuid4().hex}") as cg:
        start_grandparent_death_watcher(cg)
        threads = [t for t in cg._threads if t.thread.name == "grandparent-death-watcher"]
        # If the test runner has no resolvable grandparent (very unusual), the
        # watcher is a no-op; both shapes are valid.
        if _read_grandparent_pid() is None:
            assert threads == []
            return
        assert len(threads) == 1
        watcher_thread = threads[0].thread
        deadline = threading.Event()
        deadline.wait(timeout=_PARENT_POLL_INTERVAL_SECONDS + 1.0)
        assert watcher_thread.is_alive(), "Grandparent watcher exited unexpectedly during poll cycle"


def _wait_for_file(path: Path, timeout: float) -> bool:
    """Poll (no sleep) until ``path`` exists, returning whether it appeared."""
    appeared = False

    def _check() -> bool:
        nonlocal appeared
        appeared = path.exists()
        return appeared

    wait_for(_check, timeout=timeout, poll_interval=0.02, error_message=f"{path} never appeared")
    return appeared


def test_parent_death_watcher_sends_sigterm_when_parent_dies(tmp_path: Path) -> None:
    """End-to-end: when a watched process's parent dies, the watcher SIGTERMs it.

    Exercises the positive ``current_ppid != original_ppid -> os.kill(SIGTERM)``
    branch with real processes (no mocks):

        test (T) -> intermediate parent (P) -> watched child (C, runs the watcher)

    We kill P. C is reparented (its ppid changes), so its parent-death watcher
    must fire SIGTERM at C. C's SIGTERM handler writes a sentinel file, which we
    poll for. A bug that inverted the comparison, skipped ``os.kill``, or sent
    the wrong signal would leave the sentinel absent and fail this test.
    """
    unique = uuid4().hex
    child_pid_path = tmp_path / f"child-pid-{unique}"
    ready_path = tmp_path / f"ready-{unique}"
    sigterm_path = tmp_path / f"sigterm-{unique}"

    env = {
        **os.environ,
        "WATCHED_CHILD_PROGRAM": _WATCHED_CHILD_PROGRAM,
        "CHILD_PID_PATH": str(child_pid_path),
        "WATCHER_READY_PATH": str(ready_path),
        "WATCHER_SIGTERM_PATH": str(sigterm_path),
    }
    intermediate = subprocess.Popen(
        [sys.executable, "-c", _INTERMEDIATE_PARENT_PROGRAM],
        env=env,
    )
    child_proc: psutil.Process | None = None
    try:
        # Wait for the child to be spawned and for its watcher to be running.
        assert _wait_for_file(child_pid_path, timeout=20.0)
        assert _wait_for_file(ready_path, timeout=20.0)
        child_pid = int(child_pid_path.read_text())
        child_proc = psutil.Process(child_pid)
        assert child_proc.ppid() == intermediate.pid, "child should start parented to the intermediate"

        # Kill the intermediate parent; the child gets reparented away from it.
        intermediate.terminate()
        intermediate.wait(timeout=20.0)

        # The watcher must observe the ppid change and SIGTERM the child, whose
        # handler writes the sentinel. Poll for it rather than sleeping.
        assert _wait_for_file(sigterm_path, timeout=20.0), "watched child never received SIGTERM after its parent died"
        # The sentinel records the ppid the child observed at SIGTERM time: it
        # must differ from the (now-dead) intermediate, confirming reparenting
        # drove the watcher rather than some unrelated signal.
        observed_ppid_at_sigterm = int(sigterm_path.read_text())
        assert observed_ppid_at_sigterm != intermediate.pid
        # The child's handler exits after writing the sentinel, so the process
        # must wind down. psutil can't read a grandchild's exit code, so poll for
        # it to stop running instead. A reparented orphan is reaped by init, so a
        # zombie also counts as "no longer running".
        local_child = child_proc

        def _child_stopped() -> bool:
            try:
                if not local_child.is_running():
                    return True
                return local_child.status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return True

        wait_for(
            _child_stopped,
            timeout=20.0,
            poll_interval=0.05,
            error_message="watched child did not exit after handling SIGTERM",
        )
    finally:
        if intermediate.poll() is None:
            intermediate.kill()
            intermediate.wait(timeout=10.0)
        if child_proc is not None:
            try:
                if child_proc.is_running():
                    child_proc.kill()
            except psutil.NoSuchProcess:
                pass


def test_read_grandparent_pid_matches_process_tree() -> None:
    """Cross-check ``_read_grandparent_pid`` against psutil's real process tree.

    The helper parses ``/proc/<ppid>/status`` (Linux-only). On Linux we verify
    its result equals what psutil reports for the actual grandparent (or
    ``None`` when the grandparent is init/pid 1). On macOS there is no ``/proc``,
    so the helper's parse path can't be exercised; we assert only that it
    degrades to ``None`` and skip the cross-check.

    macOS gap: the PPid-line parsing, ``PPid == 1 -> None``, ``<= 1`` guard, and
    ``OSError -> None`` branches are only covered on Linux. There is no seam to
    inject ``/proc`` content without refactoring production, so this is the best
    real-process assertion available.
    """
    result = _read_grandparent_pid()
    if sys.platform != "linux":
        assert result is None, "without /proc the helper must return None"
        pytest.skip("/proc grandparent parsing is Linux-only; nothing to cross-check on this platform")

    parent = psutil.Process(os.getppid())
    expected_grandparent_pid = parent.ppid()
    if expected_grandparent_pid <= 1:
        assert result is None
    else:
        assert result == expected_grandparent_pid
