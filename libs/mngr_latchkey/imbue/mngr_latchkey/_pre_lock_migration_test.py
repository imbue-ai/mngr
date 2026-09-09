"""CLEANUP: delete alongside ``_pre_lock_migration.py``."""

import contextlib
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import psutil
import pytest

from imbue.mngr_latchkey._pre_lock_migration import _START_TIME_SKEW_ALLOWANCE_SECONDS
from imbue.mngr_latchkey._pre_lock_migration import _looks_like_a_forward
from imbue.mngr_latchkey._pre_lock_migration import migrate_pre_lock_forward
from imbue.mngr_latchkey._pre_lock_migration import pre_lock_forward_pid
from imbue.mngr_latchkey.store import LatchkeyForwardInfo
from imbue.mngr_latchkey.store import forward_info_path
from imbue.mngr_latchkey.store import save_forward_info


def _process_started_at() -> datetime:
    return datetime.fromtimestamp(psutil.Process().create_time(), tz=timezone.utc)


@contextlib.contextmanager
def _live_pre_lock_forward() -> Iterator[int]:
    """Yield the pid of an idling process whose argv is a ``mngr latchkey forward``.

    Only the argv shape matters: it is all the recogniser has to go on for a
    forward that holds no lock.
    """
    process = subprocess.Popen(
        [sys.executable, "-c", "import signal; signal.pause()", "mngr", "latchkey", "forward"],
    )
    try:
        yield process.pid
    finally:
        process.terminate()
        process.wait(timeout=10.0)


def test_no_record_means_nothing_to_migrate(tmp_path: Path) -> None:
    assert pre_lock_forward_pid(tmp_path) is None


def test_a_pid_that_started_after_its_record_is_not_the_forward(tmp_path: Path) -> None:
    """A pid handed to something else after the forward died must not be signalled.

    ``migrate_pre_lock_forward`` kills what this names, and takes the descendant
    tree with it, so naming a stranger is the failure that matters.
    """
    save_forward_info(
        tmp_path,
        LatchkeyForwardInfo(
            pid=os.getpid(),
            started_at=_process_started_at() - timedelta(seconds=_START_TIME_SKEW_ALLOWANCE_SECONDS + 1),
        ),
    )
    assert pre_lock_forward_pid(tmp_path) is None


def test_a_live_pid_that_is_not_a_forward_is_not_the_forward(tmp_path: Path) -> None:
    """This test process started when its record says, but is not a forward."""
    save_forward_info(tmp_path, LatchkeyForwardInfo(pid=os.getpid(), started_at=_process_started_at()))
    assert pre_lock_forward_pid(tmp_path) is None


def test_migrating_clears_the_record_even_with_nothing_to_kill(tmp_path: Path) -> None:
    """The record must not outlive the migration, or every launch re-runs it."""
    save_forward_info(tmp_path, LatchkeyForwardInfo(pid=os.getpid(), started_at=_process_started_at()))
    killed: list[int] = []
    migrate_pre_lock_forward(tmp_path, killed.append)
    assert killed == []
    assert not forward_info_path(tmp_path).is_file()


def test_migrating_kills_the_recorded_forward(tmp_path: Path) -> None:
    """A live pre-lock forward is terminated so a lock-holding one can take over."""
    with _live_pre_lock_forward() as pid:
        # Stamped now, as a pre-lock forward stamped it the moment it started.
        save_forward_info(tmp_path, LatchkeyForwardInfo(pid=pid, started_at=datetime.now(timezone.utc)))
        killed: list[int] = []
        migrate_pre_lock_forward(tmp_path, killed.append)
        assert killed == [pid]
    assert not forward_info_path(tmp_path).is_file()


@pytest.mark.parametrize(
    ("cmdline", "is_a_forward"),
    (
        pytest.param(["/usr/bin/mngr", "latchkey", "forward", "--quiet"], True, id="argv"),
        pytest.param(["mngr latchkey forward --latchkey-directory '/Users/Jane Doe/lk'"], True, id="fused-title"),
        pytest.param(["mngr", "latchkey", "forward", "", ""], True, id="argv-with-blank-slots"),
        pytest.param(["/usr/bin/mngr", "observe"], False, id="another-subcommand"),
        pytest.param(["/usr/bin/manager", "latchkey", "forward"], False, id="mngr-as-substring"),
        pytest.param(["/opt/mngr-foo", "latchkey", "forward"], False, id="mngr-as-prefix"),
    ),
)
def test_only_a_mngr_latchkey_forward_is_recognised(cmdline: list[str], is_a_forward: bool) -> None:
    """The title ``mngr`` leaves behind is what this has to read, in either shape.

    ``setproctitle`` overwrites argv, so :meth:`psutil.Process.cmdline` hands
    back one fused string on macOS and a space-split list on Linux.
    """
    assert _looks_like_a_forward(cmdline) is is_a_forward
