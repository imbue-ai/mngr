import threading
import time
from pathlib import Path
from uuid import uuid4

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.create_attempt_discard import CreateAttemptDiscardStatus
from imbue.minds.desktop_client.create_attempt_discard import delete_discard
from imbue.minds.desktop_client.create_attempt_discard import read_discard
from imbue.minds.desktop_client.create_attempt_discard import read_discard_log_chunk
from imbue.minds.desktop_client.create_attempt_discard import start_discard_of_host
from imbue.minds.desktop_client.create_attempt_discard import start_discard_without_host


def _paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(data_dir=tmp_path / "minds-data")


def _create_attempt_id() -> str:
    return f"create-attempt-{uuid4().hex}"


def _write_fake_mngr(tmp_path: Path, exit_code: int) -> tuple[str, Path]:
    """A fake ``mngr`` that logs its argv, prints a line, and exits ``exit_code``."""
    calls_path = tmp_path / "discard-mngr-calls.log"
    calls_path.write_text("")
    script_path = tmp_path / "fake-discard-mngr"
    script_path.write_text(f'#!/bin/bash\necho "$@" >> "{calls_path}"\necho "destroy output line"\nexit {exit_code}\n')
    script_path.chmod(0o755)
    return str(script_path), calls_path


def _wait_for_terminal_status(create_attempt_id: str, paths: WorkspacePaths, deadline_seconds: float = 10.0) -> str:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        record = read_discard(create_attempt_id, paths)
        if record is not None and record.status is not CreateAttemptDiscardStatus.RUNNING:
            return str(record.status)
        threading.Event().wait(timeout=0.05)
    raise AssertionError("discard never reached a terminal status")


def test_read_discard_returns_none_without_a_dir(tmp_path: Path) -> None:
    assert read_discard(_create_attempt_id(), _paths(tmp_path)) is None


def test_discard_without_host_is_immediately_done_with_the_message_logged(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    create_attempt_id = _create_attempt_id()
    record = start_discard_without_host(create_attempt_id, paths, "No leftover host to clean up.")
    assert record.status is CreateAttemptDiscardStatus.DONE
    read_back = read_discard(create_attempt_id, paths)
    assert read_back is not None
    assert read_back.status is CreateAttemptDiscardStatus.DONE
    content, _next_offset = read_discard_log_chunk(create_attempt_id, paths, 0)
    assert b"No leftover host to clean up." in content


def test_discard_of_host_runs_mngr_destroy_and_reports_done_on_exit_zero(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    create_attempt_id = _create_attempt_id()
    mngr_binary, calls_path = _write_fake_mngr(tmp_path, exit_code=0)
    start_discard_of_host(create_attempt_id, paths, host_id="host-1234", provider_name="lima", mngr_binary=mngr_binary)
    assert _wait_for_terminal_status(create_attempt_id, paths) == "DONE"
    calls = [line for line in calls_path.read_text().splitlines() if line]
    assert calls == ["destroy @host-1234.lima --force"]
    content, _next_offset = read_discard_log_chunk(create_attempt_id, paths, 0)
    assert b"destroy output line" in content


def test_discard_of_host_reports_failed_on_nonzero_exit(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    create_attempt_id = _create_attempt_id()
    mngr_binary, _calls_path = _write_fake_mngr(tmp_path, exit_code=3)
    start_discard_of_host(create_attempt_id, paths, host_id="host-1234", provider_name="lima", mngr_binary=mngr_binary)
    assert _wait_for_terminal_status(create_attempt_id, paths) == "FAILED"


def test_failed_discard_can_be_retried_with_a_truncated_log(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    create_attempt_id = _create_attempt_id()
    failing_binary, _failing_calls = _write_fake_mngr(tmp_path, exit_code=3)
    start_discard_of_host(
        create_attempt_id, paths, host_id="host-1234", provider_name="lima", mngr_binary=failing_binary
    )
    assert _wait_for_terminal_status(create_attempt_id, paths) == "FAILED"

    succeeding_dir = tmp_path / "second"
    succeeding_dir.mkdir()
    succeeding_binary, _ok_calls = _write_fake_mngr(succeeding_dir, exit_code=0)
    start_discard_of_host(
        create_attempt_id, paths, host_id="host-1234", provider_name="lima", mngr_binary=succeeding_binary
    )
    assert _wait_for_terminal_status(create_attempt_id, paths) == "DONE"
    # The retry truncated the previous run's log, so exactly one output line remains.
    content, _next_offset = read_discard_log_chunk(create_attempt_id, paths, 0)
    assert content.count(b"destroy output line") == 1


def test_read_discard_log_chunk_advances_offsets(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    create_attempt_id = _create_attempt_id()
    start_discard_without_host(create_attempt_id, paths, "first line")
    content, next_offset = read_discard_log_chunk(create_attempt_id, paths, 0)
    assert content == b"first line\n"
    tail, final_offset = read_discard_log_chunk(create_attempt_id, paths, next_offset)
    assert tail == b""
    assert final_offset == next_offset


def test_delete_discard_is_idempotent(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    create_attempt_id = _create_attempt_id()
    start_discard_without_host(create_attempt_id, paths, "cleanup")
    assert delete_discard(create_attempt_id, paths) is True
    assert read_discard(create_attempt_id, paths) is None
    assert delete_discard(create_attempt_id, paths) is False
