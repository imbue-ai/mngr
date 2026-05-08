"""Unit tests for the detached destroy lifecycle.

We avoid actually invoking ``mngr destroy`` -- the bash command the spawn
helper builds is exercised by replacing the binary at the call boundary
with a tiny shell script that writes to stdout/stderr and exits 0 or 1.
That gives us deterministic coverage of the pid + log capture + status
table without any live mngr state.
"""

import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

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

    ``mngr destroy <id> -f`` and ``mngr list ... | mngr destroy -f -`` both
    end up running this binary, which is enough for the destroy helper's
    contract (any non-zero exit leaves the agent in the resolver).

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


def test_build_destroy_command_with_host_id_does_fanout() -> None:
    agent_id = AgentId.generate()
    command = _build_destroy_command(agent_id, host_id="host-abc123")
    assert command[0] == "bash"
    assert command[1] == "-c"
    # Pipe-fanout shape: list ... | destroy -f -
    assert 'host.id == "host-abc123"' in command[2]
    assert "destroy -f -" in command[2]


def test_build_destroy_command_without_host_id_falls_back_to_single() -> None:
    agent_id = AgentId.generate()
    command = _build_destroy_command(agent_id, host_id=None)
    assert command[0] == "bash"
    # Single-agent destroy: no pipe, just `mngr destroy <id> -f`.
    assert "destroy -f -" not in command[2]
    assert f"destroy {agent_id} -f" in command[2]


def test_start_destroy_writes_pid_file_and_log(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=0, stdout="destroyed agent\n")
    record = start_destroy(agent_id, paths, host_id=None, env=_path_with_fake_mngr(fake))

    pid_file = tmp_path / "destroying" / str(agent_id) / "pid"
    log_file = tmp_path / "destroying" / str(agent_id) / "output.log"
    assert pid_file.read_text().strip() == str(record.pid)
    assert _wait_for_pid_exit(record.pid)
    assert log_file.read_text() == "destroyed agent\n"


def test_read_destroying_status_running_when_pid_alive(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    # Sleep long enough for the next read but not so long that the test gets slow.
    sleeper = tmp_path / "bin" / "mngr"
    sleeper.parent.mkdir(exist_ok=True)
    sleeper.write_text("#!/bin/bash\nsleep 2\n")
    sleeper.chmod(0o755)
    record = start_destroy(agent_id, paths, host_id=None, env=_path_with_fake_mngr(sleeper))
    try:
        seen = read_destroying(agent_id, paths, agent_in_resolver=True)
        assert seen is not None
        assert seen.status == DestroyingStatus.RUNNING
        assert seen.pid_alive is True
    finally:
        # Best-effort cleanup so the test process doesn't leave a sleeper running.
        try:
            os.kill(record.pid, 15)
        except ProcessLookupError:
            pass
        _wait_for_pid_exit(record.pid)


def test_read_destroying_status_done_when_pid_dead_and_agent_gone(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=0)
    record = start_destroy(agent_id, paths, host_id=None, env=_path_with_fake_mngr(fake))
    assert _wait_for_pid_exit(record.pid)
    seen = read_destroying(agent_id, paths, agent_in_resolver=False)
    assert seen is not None
    assert seen.status == DestroyingStatus.DONE
    assert seen.pid_alive is False


def test_read_destroying_status_failed_when_pid_dead_but_agent_still_present(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=1, stderr="boom\n")
    record = start_destroy(agent_id, paths, host_id=None, env=_path_with_fake_mngr(fake))
    assert _wait_for_pid_exit(record.pid)
    seen = read_destroying(agent_id, paths, agent_in_resolver=True)
    assert seen is not None
    assert seen.status == DestroyingStatus.FAILED


def test_read_destroying_returns_none_when_no_directory(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    assert read_destroying(AgentId.generate(), paths, agent_in_resolver=False) is None


def test_start_destroy_is_idempotent_while_running(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    sleeper = tmp_path / "bin" / "mngr"
    sleeper.parent.mkdir(exist_ok=True)
    sleeper.write_text("#!/bin/bash\nsleep 2\n")
    sleeper.chmod(0o755)
    first = start_destroy(agent_id, paths, host_id=None, env=_path_with_fake_mngr(sleeper))
    try:
        second = start_destroy(agent_id, paths, host_id=None, env=_path_with_fake_mngr(sleeper))
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
    agent_a = AgentId.generate()
    agent_b = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=0)
    record_a = start_destroy(agent_a, paths, host_id=None, env=_path_with_fake_mngr(fake))
    record_b = start_destroy(agent_b, paths, host_id=None, env=_path_with_fake_mngr(fake))
    assert _wait_for_pid_exit(record_a.pid)
    assert _wait_for_pid_exit(record_b.pid)
    listing = list_destroying(paths, frozenset({agent_a}))
    assert agent_a in listing
    assert agent_b in listing
    # agent_a is "still in resolver" → FAILED, agent_b is gone → DONE.
    assert listing[agent_a].status == DestroyingStatus.FAILED
    assert listing[agent_b].status == DestroyingStatus.DONE


def test_delete_destroying_is_idempotent(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=0)
    record = start_destroy(agent_id, paths, host_id=None, env=_path_with_fake_mngr(fake))
    assert _wait_for_pid_exit(record.pid)
    assert delete_destroying(agent_id, paths) is True
    assert delete_destroying(agent_id, paths) is False
    assert not (tmp_path / "destroying" / str(agent_id)).exists()


def test_read_log_chunk_returns_tail_from_offset(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    fake = _make_fake_mngr(tmp_path, exit_code=0, stdout="hello world\n")
    record = start_destroy(agent_id, paths, host_id=None, env=_path_with_fake_mngr(fake))
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


def test_idempotent_after_failure_overwrites_log(tmp_path: Path) -> None:
    """A Retry overwrites the previous run's log so the user sees the new attempt fresh."""
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId.generate()
    failing = _make_fake_mngr(tmp_path, exit_code=1, stderr="first run boom\n")
    first = start_destroy(agent_id, paths, host_id=None, env=_path_with_fake_mngr(failing))
    assert _wait_for_pid_exit(first.pid)
    log_path = tmp_path / "destroying" / str(agent_id) / "output.log"
    assert b"first run boom" in log_path.read_bytes()

    succeeding = _make_fake_mngr(tmp_path, exit_code=0, stdout="second run ok\n")
    second = start_destroy(agent_id, paths, host_id=None, env=_path_with_fake_mngr(succeeding))
    assert _wait_for_pid_exit(second.pid)
    after = log_path.read_bytes()
    assert b"first run boom" not in after
    assert b"second run ok" in after


@pytest.fixture(autouse=True)
def _cleanup_tmp_destroying(tmp_path: Path) -> Iterator[None]:
    """Best-effort tmp dir cleanup after tests that may leave background pids."""
    yield
    destroy_root = tmp_path / "destroying"
    if destroy_root.exists():
        shutil.rmtree(destroy_root, ignore_errors=True)
