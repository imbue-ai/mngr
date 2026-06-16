"""Unit tests for the detached destroy lifecycle.

We avoid actually invoking ``mngr destroy`` -- the bash command the spawn
helper builds is exercised by replacing the binary at the call boundary
with a tiny shell script that writes to stdout/stderr and exits 0 or 1.
That gives us deterministic coverage of the pid + log capture and the
exit-code-driven status table without any live mngr state.
"""

import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.destroying import DestroyingStatus
from imbue.minds.desktop_client.destroying import _build_destroy_command
from imbue.minds.desktop_client.destroying import _is_pid_alive
from imbue.minds.desktop_client.destroying import delete_destroying
from imbue.minds.desktop_client.destroying import list_destroying
from imbue.minds.desktop_client.destroying import read_destroying
from imbue.minds.desktop_client.destroying import read_log_chunk
from imbue.minds.desktop_client.destroying import start_destroy
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId


def _wait_for_pid_exit(pid: int, timeout: float = 5.0, poll: float = 0.05) -> bool:
    """Block until ``pid`` is no longer alive (or ``timeout`` elapses).

    Uses ``destroying._is_pid_alive`` so that zombie children (the test
    process is the Popen parent in tests; the destroy bash exits to
    zombie state until reaped) get reaped via ``os.waitpid`` and
    correctly transition to "not alive".
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_pid_alive(pid):
            return True
        time.sleep(poll)
    return False


def _make_fake_mngr(tmp_path: Path, exit_code: int, stdout: str = "", stderr: str = "") -> Path:
    """Write a tiny bash script that pretends to be ``mngr`` and exits with ``exit_code``.

    The destroy command (``mngr list ... | mngr destroy -f -``) ends up running
    this binary, which is enough for the destroy helper's contract (the wrapper
    records whatever exit code the pipeline returns).

    stdout/stderr are passed through ``printf '%b'`` so that ``\\n`` in the
    Python string is interpreted as a real newline by bash (rather than a
    literal backslash-n).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "mngr"

    # Quote each payload as a single-quoted bash string with embedded ' escaped.
    def _bash_squote(value: str) -> str:
        return "'" + value.replace("'", "'\\''") + "'"

    script = (
        f"#!/bin/bash\nprintf '%b' {_bash_squote(stdout)}\nprintf '%b' {_bash_squote(stderr)} >&2\nexit {exit_code}\n"
    )
    fake.write_text(script)
    fake.chmod(0o755)
    return fake


def _path_with_fake_mngr(fake_bin: Path) -> dict[str, str]:
    """Build an env that prepends ``fake_bin``'s parent dir to PATH so ``mngr`` resolves to the fake."""
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin.parent}:{env.get('PATH', '')}"
    return env


def _destroying_dir(tmp_path: Path, agent_id: AgentId) -> Path:
    return tmp_path / "destroying" / str(agent_id)


def test_build_destroy_command_fans_out_over_the_whole_host(tmp_path: Path) -> None:
    host_id = HostId.generate()
    result_path = tmp_path / "result"
    command = _build_destroy_command(host_id, result_path=result_path)
    assert command[0] == "bash"
    assert command[1] == "-c"
    # Pipe-fanout shape: list every agent on the host | destroy -f -. There is
    # deliberately no single-agent path, so destroying a minds workspace tears
    # down the whole host (workspace agent + system-services).
    assert f'host.id == "{host_id}"' in command[2]
    assert "destroy -f -" in command[2]
    # pipefail so a failed `mngr list` is recorded rather than masked, and the
    # wrapper records the pipeline's exit code to result_path.
    assert "set -o pipefail" in command[2]
    assert str(result_path) in command[2]


def test_start_destroy_writes_pid_and_log(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=0, stdout="destroyed host\n")
    record = start_destroy(
        agent_id, paths, host_id=HostId.generate(), env=_path_with_fake_mngr(fake), mngr_binary="mngr"
    )

    pid_file = _destroying_dir(tmp_path, agent_id) / "pid"
    log_file = _destroying_dir(tmp_path, agent_id) / "output.log"
    assert pid_file.read_text().strip() == str(record.pid)
    assert _wait_for_pid_exit(record.pid)
    assert log_file.read_text() == "destroyed host\n"


def test_start_destroy_records_exit_code(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=3)
    record = start_destroy(
        agent_id, paths, host_id=HostId.generate(), env=_path_with_fake_mngr(fake), mngr_binary="mngr"
    )
    assert _wait_for_pid_exit(record.pid)
    # The wrapper writes `result` before it exits, so once the pid is gone the
    # recorded exit code is already on disk.
    result_file = _destroying_dir(tmp_path, agent_id) / "result"
    assert result_file.read_text().strip() == "3"
    seen = read_destroying(agent_id, paths)
    assert seen is not None
    assert seen.exit_code == 3
    assert seen.status == DestroyingStatus.FAILED


def test_read_destroying_running_when_pid_alive(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    # Sleep long enough for the next read but not so long that the test gets slow.
    sleeper = tmp_path / "bin" / "mngr"
    sleeper.parent.mkdir(exist_ok=True)
    sleeper.write_text("#!/bin/bash\nsleep 2\n")
    sleeper.chmod(0o755)
    record = start_destroy(
        agent_id, paths, host_id=HostId.generate(), env=_path_with_fake_mngr(sleeper), mngr_binary="mngr"
    )
    try:
        seen = read_destroying(agent_id, paths)
        assert seen is not None
        assert seen.status == DestroyingStatus.RUNNING
        assert seen.pid_alive is True
        assert seen.exit_code is None
    finally:
        # Best-effort cleanup so the test process doesn't leave a sleeper running.
        try:
            os.kill(record.pid, 15)
        except ProcessLookupError:
            pass
        _wait_for_pid_exit(record.pid)


def test_read_destroying_done_when_exit_code_zero(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=0)
    record = start_destroy(
        agent_id, paths, host_id=HostId.generate(), env=_path_with_fake_mngr(fake), mngr_binary="mngr"
    )
    assert _wait_for_pid_exit(record.pid)
    seen = read_destroying(agent_id, paths)
    assert seen is not None
    assert seen.status == DestroyingStatus.DONE
    assert seen.pid_alive is False
    assert seen.exit_code == 0


def test_read_destroying_failed_when_exit_code_nonzero(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=1, stderr="boom\n")
    record = start_destroy(
        agent_id, paths, host_id=HostId.generate(), env=_path_with_fake_mngr(fake), mngr_binary="mngr"
    )
    assert _wait_for_pid_exit(record.pid)
    seen = read_destroying(agent_id, paths)
    assert seen is not None
    assert seen.status == DestroyingStatus.FAILED
    assert seen.exit_code == 1


def test_recorded_result_overrides_a_live_pid(tmp_path: Path) -> None:
    """A recorded exit code is authoritative even while the pid is still alive.

    This is the override: without the result file the status would derive
    from pid liveness (RUNNING), but the recorded outcome wins -- so a
    succeeded destroy never lingers as Destroying and never flips to
    spurious Failed waiting on discovery.
    """
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    dir_path = _destroying_dir(tmp_path, agent_id)
    dir_path.mkdir(parents=True)
    # The test process's own pid -- genuinely alive.
    (dir_path / "pid").write_text(f"{os.getpid()}\n")

    (dir_path / "result").write_text("0\n")
    done = read_destroying(agent_id, paths)
    assert done is not None
    assert done.pid_alive is True
    assert done.status == DestroyingStatus.DONE

    (dir_path / "result").write_text("1\n")
    failed = read_destroying(agent_id, paths)
    assert failed is not None
    assert failed.pid_alive is True
    assert failed.status == DestroyingStatus.FAILED


def test_read_destroying_failed_when_pid_dead_and_no_result(tmp_path: Path) -> None:
    """A wrapper that died before recording its outcome reads FAILED, not DONE.

    Guards against the silent-orphan reopen case: an interrupted destroy
    is surfaced for inspection rather than being mistaken for success.
    """
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=0)
    record = start_destroy(
        agent_id, paths, host_id=HostId.generate(), env=_path_with_fake_mngr(fake), mngr_binary="mngr"
    )
    assert _wait_for_pid_exit(record.pid)
    # Delete the recorded result to simulate a wrapper that was killed before
    # it could record its outcome.
    (_destroying_dir(tmp_path, agent_id) / "result").unlink()
    seen = read_destroying(agent_id, paths)
    assert seen is not None
    assert seen.exit_code is None
    assert seen.pid_alive is False
    assert seen.status == DestroyingStatus.FAILED


def test_is_pid_alive_rejects_recycled_pid_via_create_time() -> None:
    pid = os.getpid()
    actual_create_time = psutil.Process(pid).create_time()
    assert _is_pid_alive(pid) is True
    assert _is_pid_alive(pid, expected_create_time=actual_create_time) is True
    # A create_time far from the real one models the OS recycling the pid
    # onto an unrelated process while minds was closed.
    assert _is_pid_alive(pid, expected_create_time=actual_create_time - 3600.0) is False


def test_read_destroying_returns_none_when_no_directory(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    assert read_destroying(AgentId.generate(), paths) is None


def test_start_destroy_is_idempotent_while_running(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    sleeper = tmp_path / "bin" / "mngr"
    sleeper.parent.mkdir(exist_ok=True)
    sleeper.write_text("#!/bin/bash\nsleep 2\n")
    sleeper.chmod(0o755)
    first = start_destroy(agent_id, paths, host_id=host_id, env=_path_with_fake_mngr(sleeper), mngr_binary="mngr")
    try:
        second = start_destroy(agent_id, paths, host_id=host_id, env=_path_with_fake_mngr(sleeper), mngr_binary="mngr")
        assert second.pid == first.pid
        assert second.status == DestroyingStatus.RUNNING
    finally:
        try:
            os.kill(first.pid, 15)
        except ProcessLookupError:
            pass
        _wait_for_pid_exit(first.pid)


def test_list_destroying_walks_dir_and_picks_up_each_agent(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_failed = AgentId.generate()
    agent_done = AgentId.generate()
    failing = _make_fake_mngr(tmp_path, exit_code=1)
    record_failed = start_destroy(
        agent_failed, paths, host_id=HostId.generate(), env=_path_with_fake_mngr(failing), mngr_binary="mngr"
    )
    assert _wait_for_pid_exit(record_failed.pid)
    succeeding = _make_fake_mngr(tmp_path, exit_code=0)
    record_done = start_destroy(
        agent_done, paths, host_id=HostId.generate(), env=_path_with_fake_mngr(succeeding), mngr_binary="mngr"
    )
    assert _wait_for_pid_exit(record_done.pid)
    listing = list_destroying(paths)
    assert agent_failed in listing
    assert agent_done in listing
    # Status comes from each wrapper's recorded exit code, not the resolver.
    assert listing[agent_failed].status == DestroyingStatus.FAILED
    assert listing[agent_done].status == DestroyingStatus.DONE


def test_delete_destroying_is_idempotent(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=0)
    record = start_destroy(
        agent_id, paths, host_id=HostId.generate(), env=_path_with_fake_mngr(fake), mngr_binary="mngr"
    )
    assert _wait_for_pid_exit(record.pid)
    assert delete_destroying(agent_id, paths) is True
    assert delete_destroying(agent_id, paths) is False
    assert not _destroying_dir(tmp_path, agent_id).exists()


def test_read_log_chunk_returns_tail_from_offset(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=0, stdout="hello world\n")
    record = start_destroy(
        agent_id, paths, host_id=HostId.generate(), env=_path_with_fake_mngr(fake), mngr_binary="mngr"
    )
    assert _wait_for_pid_exit(record.pid)
    content, next_offset = read_log_chunk(agent_id, paths, offset=0)
    assert content == b"hello world\n"
    assert next_offset == len(b"hello world\n")
    # Reading from EOF returns empty bytes and the same offset.
    empty, same_offset = read_log_chunk(agent_id, paths, offset=next_offset)
    assert empty == b""
    assert same_offset == next_offset


def test_read_log_chunk_raises_filenotfound_when_no_record(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        read_log_chunk(AgentId.generate(), paths, offset=0)


def test_idempotent_after_failure_overwrites_log_and_result(tmp_path: Path) -> None:
    """A Retry overwrites the previous run's log and stale result so the user sees the new attempt fresh."""
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    failing = _make_fake_mngr(tmp_path, exit_code=1, stderr="first run boom\n")
    first = start_destroy(agent_id, paths, host_id=host_id, env=_path_with_fake_mngr(failing), mngr_binary="mngr")
    assert _wait_for_pid_exit(first.pid)
    log_path = _destroying_dir(tmp_path, agent_id) / "output.log"
    assert b"first run boom" in log_path.read_bytes()
    first_seen = read_destroying(agent_id, paths)
    assert first_seen is not None and first_seen.status == DestroyingStatus.FAILED

    succeeding = _make_fake_mngr(tmp_path, exit_code=0, stdout="second run ok\n")
    second = start_destroy(agent_id, paths, host_id=host_id, env=_path_with_fake_mngr(succeeding), mngr_binary="mngr")
    assert _wait_for_pid_exit(second.pid)
    after = log_path.read_bytes()
    assert b"first run boom" not in after
    assert b"second run ok" in after
    second_seen = read_destroying(agent_id, paths)
    assert second_seen is not None and second_seen.status == DestroyingStatus.DONE


@pytest.fixture(autouse=True)
def _cleanup_tmp_destroying(tmp_path: Path) -> Iterator[None]:
    """Best-effort tmp dir cleanup after tests that may leave background pids."""
    yield
    destroy_root = tmp_path / "destroying"
    if destroy_root.exists():
        shutil.rmtree(destroy_root, ignore_errors=True)
