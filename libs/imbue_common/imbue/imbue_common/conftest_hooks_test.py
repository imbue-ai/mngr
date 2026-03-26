"""Tests for the global test lock helpers in conftest_hooks."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from imbue.imbue_common.conftest_hooks import _acquire_global_test_lock
from imbue.imbue_common.conftest_hooks import _compute_lock_deadline
from imbue.imbue_common.conftest_hooks import _compute_max_duration
from imbue.imbue_common.conftest_hooks import _is_process_alive
from imbue.imbue_common.conftest_hooks import _read_lock_info
from imbue.imbue_common.conftest_hooks import _try_break_stale_lock
from imbue.imbue_common.conftest_hooks import _verify_lock_inode
from imbue.imbue_common.conftest_hooks import _write_lock_info

# --- _is_process_alive ---


def test_current_process_is_alive() -> None:
    assert _is_process_alive(os.getpid()) is True


def test_nonexistent_pid_is_not_alive() -> None:
    # PID 2**22 is unlikely to exist (and is within the valid PID range).
    assert _is_process_alive(2**22) is False


# --- _read_lock_info / _write_lock_info ---


def test_lock_info_round_trip_with_deadline(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    lock_file.touch()
    with lock_file.open("r+") as fh:
        _write_lock_info(fh, pid=12345, deadline=1000.5)

    info = _read_lock_info(lock_file)
    assert info is not None
    assert info["pid"] == 12345
    assert info["deadline"] == 1000.5


def test_lock_info_round_trip_without_deadline(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    lock_file.touch()
    with lock_file.open("r+") as fh:
        _write_lock_info(fh, pid=99, deadline=None)

    info = _read_lock_info(lock_file)
    assert info is not None
    assert info["pid"] == 99
    assert "deadline" not in info


def test_read_lock_info_empty_file(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    lock_file.touch()
    assert _read_lock_info(lock_file) is None


def test_read_lock_info_missing_file(tmp_path: Path) -> None:
    assert _read_lock_info(tmp_path / "no_such_file") is None


def test_read_lock_info_invalid_json(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    lock_file.write_text("not json{")
    assert _read_lock_info(lock_file) is None


def test_read_lock_info_non_dict_json(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    lock_file.write_text("[1, 2, 3]")
    assert _read_lock_info(lock_file) is None


# --- _compute_max_duration ---


def test_max_duration_explicit_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_MAX_DURATION_SECONDS", "42")
    assert _compute_max_duration() == 42.0


def test_max_duration_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IS_RELEASE", "1")
    monkeypatch.delenv("PYTEST_MAX_DURATION_SECONDS", raising=False)
    assert _compute_max_duration() == 600.0


def test_max_duration_acceptance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IS_ACCEPTANCE", "1")
    monkeypatch.delenv("PYTEST_MAX_DURATION_SECONDS", raising=False)
    monkeypatch.delenv("IS_RELEASE", raising=False)
    assert _compute_max_duration() == 360.0


def test_max_duration_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "1")
    monkeypatch.delenv("PYTEST_MAX_DURATION_SECONDS", raising=False)
    monkeypatch.delenv("IS_RELEASE", raising=False)
    monkeypatch.delenv("IS_ACCEPTANCE", raising=False)
    assert _compute_max_duration() == 150.0


def test_max_duration_local_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_MAX_DURATION_SECONDS", raising=False)
    monkeypatch.delenv("IS_RELEASE", raising=False)
    monkeypatch.delenv("IS_ACCEPTANCE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    assert _compute_max_duration() == 300.0


# --- _compute_lock_deadline ---


def test_lock_deadline_none_without_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_MAX_DURATION_SECONDS", raising=False)
    assert _compute_lock_deadline(1000.0) is None


def test_lock_deadline_computed_with_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_MAX_DURATION_SECONDS", "120")
    deadline = _compute_lock_deadline(1000.0)
    assert deadline is not None
    # 1000 + 120 + 60 (grace)
    assert deadline == 1180.0


# --- _try_break_stale_lock ---


def test_break_stale_lock_dead_pid(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    # Use a PID that is almost certainly not alive.
    lock_file.write_text(json.dumps({"pid": 2**22}))
    assert _try_break_stale_lock(lock_file) is True
    assert not lock_file.exists()


def test_break_stale_lock_alive_pid_no_deadline(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    lock_file.write_text(json.dumps({"pid": os.getpid()}))
    assert _try_break_stale_lock(lock_file) is False
    assert lock_file.exists()


def test_break_stale_lock_alive_pid_future_deadline(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    future = time.time() + 9999
    lock_file.write_text(json.dumps({"pid": os.getpid(), "deadline": future}))
    assert _try_break_stale_lock(lock_file) is False
    assert lock_file.exists()


def test_break_stale_lock_empty_file(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    lock_file.touch()
    assert _try_break_stale_lock(lock_file) is False


def test_break_stale_lock_missing_file(tmp_path: Path) -> None:
    assert _try_break_stale_lock(tmp_path / "no_such_file") is False


def test_break_stale_lock_expired_deadline_kills(tmp_path: Path) -> None:
    """Start a real subprocess, set an expired deadline, and verify it gets killed."""
    lock_file = tmp_path / "lock"
    # Start a subprocess that blocks forever (waiting on stdin).
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
    )
    try:
        past = time.time() - 10
        lock_file.write_text(json.dumps({"pid": proc.pid, "deadline": past}))
        assert _try_break_stale_lock(lock_file) is True
        assert not lock_file.exists()
        # The process should be dead (SIGKILL was sent).
        proc.wait(timeout=5)
    finally:
        proc.kill()
        proc.wait()


# --- _verify_lock_inode ---


def test_verify_inode_matching(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    lock_file.touch()
    with lock_file.open("r+") as fh:
        assert _verify_lock_inode(fh, lock_file) is True


def test_verify_inode_after_recreate(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    lock_file.touch()
    with lock_file.open("r+") as fh:
        # Delete and recreate to get a different inode.
        lock_file.unlink()
        lock_file.touch()
        assert _verify_lock_inode(fh, lock_file) is False


def test_verify_inode_missing_path(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    lock_file.touch()
    with lock_file.open("r+") as fh:
        lock_file.unlink()
        assert _verify_lock_inode(fh, lock_file) is False


# --- _acquire_global_test_lock ---


def test_acquire_lock_basic(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    handle = _acquire_global_test_lock(lock_file)
    try:
        # We should be able to write to the handle.
        _write_lock_info(handle, os.getpid(), None)
        info = _read_lock_info(lock_file)
        assert info is not None
        assert info["pid"] == os.getpid()
    finally:
        handle.close()


def test_acquire_lock_after_stale_dead_pid(tmp_path: Path) -> None:
    lock_file = tmp_path / "lock"
    lock_file.write_text(json.dumps({"pid": 2**22}))

    # The stale PID detection will unlink the file before attempting flock.
    handle = _acquire_global_test_lock(lock_file)
    try:
        assert handle is not None
    finally:
        handle.close()
