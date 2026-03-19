import os
import shutil
import subprocess
import tempfile
import threading
import time
import types
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from imbue.mng.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mng.utils.testing import init_git_repo_with_config
from imbue.mng_mind import event_watcher as event_watcher_module

register_plugin_test_fixtures(globals())


def reset_loguru_impl() -> Generator[None, None, None]:
    """Reset loguru handlers before and after each test to prevent handler leakage."""
    logger.remove()
    yield
    logger.remove()


def isolate_tmux_server_impl(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    """Give each test its own isolated tmux server.

    Overrides the version from plugin_testing to use subprocess.run for cleanup
    instead of ConcurrencyGroup, which raises ProcessSetupError when no tmux
    server was started (common for unit tests that don't create tmux sessions).
    """
    tmux_tmpdir = Path(tempfile.mkdtemp(prefix="mng-tmux-", dir="/tmp"))
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_tmpdir))
    monkeypatch.delenv("TMUX", raising=False)

    yield

    tmux_tmpdir_str = str(tmux_tmpdir)
    assert tmux_tmpdir_str.startswith("/tmp/mng-tmux-"), (
        f"TMUX_TMPDIR safety check failed! Expected /tmp/mng-tmux-* path but got: {tmux_tmpdir_str}. "
        "Refusing to run 'tmux kill-server' to avoid killing the real tmux server."
    )
    socket_path = Path(tmux_tmpdir_str) / f"tmux-{os.getuid()}" / "default"
    kill_env = os.environ.copy()
    kill_env.pop("TMUX", None)
    kill_env["TMUX_TMPDIR"] = tmux_tmpdir_str
    try:
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            capture_output=True,
            env=kill_env,
        )
    except OSError:
        logger.debug("tmux kill-server failed (expected when no tmux session was started)")
    shutil.rmtree(tmux_tmpdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_loguru() -> Generator[None, None, None]:
    yield from reset_loguru_impl()


@pytest.fixture(autouse=True)
def _isolate_tmux_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    yield from isolate_tmux_server_impl(monkeypatch)


class StubCommandResult:
    """Concrete test double for command execution results."""

    def __init__(self, *, success: bool = True, stderr: str = "", stdout: str = "") -> None:
        self.success = success
        self.stderr = stderr
        self.stdout = stdout


class StubHost:
    """Concrete test double for OnlineHostInterface that records operations."""

    def __init__(
        self,
        host_dir: Path = Path("/tmp/mng-test/host"),
        command_results: dict[str, StubCommandResult] | None = None,
        text_file_contents: dict[str, str] | None = None,
        execute_mkdir: bool = False,
    ) -> None:
        self.host_dir = host_dir
        self.executed_commands: list[str] = []
        self.written_files: list[tuple[Path, bytes, str]] = []
        self.written_text_files: list[tuple[Path, str]] = []
        self._command_results = command_results or {}
        self._text_file_contents = text_file_contents or {}
        self._execute_mkdir = execute_mkdir

    def execute_command(self, command: str, **kwargs: Any) -> StubCommandResult:
        self.executed_commands.append(command)
        if self._execute_mkdir and "mkdir -p" in command:
            path = command.split("mkdir -p ")[1].strip("'\"")
            Path(path).mkdir(parents=True, exist_ok=True)
        for pattern, result in self._command_results.items():
            if pattern in command:
                return result
        if "&& pwd" in command and "cd " in command:
            path = command.split("cd ")[1].split(" &&")[0].strip("'\"")
            return StubCommandResult(stdout=path + "\n")
        return StubCommandResult()

    def read_text_file(self, path: Path) -> str:
        for pattern, content in self._text_file_contents.items():
            if pattern in str(path):
                return content
        raise FileNotFoundError(f"No stub content for {path}")

    def write_file(self, path: Path, content: bytes, mode: str = "0644") -> None:
        self.written_files.append((path, content, mode))

    def write_text_file(self, path: Path, content: str) -> None:
        self.written_text_files.append((path, content))


@pytest.fixture()
def stub_host() -> StubHost:
    """Provide a fresh StubHost instance."""
    return StubHost()


@pytest.fixture()
def temp_git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repo with an initial commit and local git config."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    init_git_repo_with_config(repo_dir)
    return repo_dir


def create_fake_mng_binary(bin_dir: Path) -> Path:
    """Create a fake mng binary at <bin_dir>/mng."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    mng_bin = bin_dir / "mng"
    mng_bin.write_text('#!/bin/bash\nexec mng "$@"\n')
    mng_bin.chmod(0o755)
    return mng_bin


class SyntheticLoopEnv:
    """Pre-built environment for _run_synthetic_events_loop tests.

    Use the ``synthetic_loop_env`` fixture to create instances.
    """

    mind_state_dir: Path
    event_buffer: list[str]
    buffer_lock: threading.Lock
    stop_event: threading.Event
    last_real_event_monotonic: list[float]


def _create_synthetic_loop_env(mind_state_dir: Path) -> SyntheticLoopEnv:
    """Create a SyntheticLoopEnv with fresh mutable state."""
    env = SyntheticLoopEnv()
    env.mind_state_dir = mind_state_dir
    env.event_buffer = []
    env.buffer_lock = threading.Lock()
    env.stop_event = threading.Event()
    env.last_real_event_monotonic = [time.monotonic()]
    return env


@pytest.fixture()
def synthetic_loop_env(tmp_path: Path) -> SyntheticLoopEnv:
    """Create an environment for synthetic events loop tests.

    Creates the mind state dir with the onboarding marker already present
    (so tests don't need to handle onboarding unless explicitly testing it).
    """
    mind_state_dir = tmp_path / "mind"
    mind_state_dir.mkdir(parents=True)
    (mind_state_dir / ".onboarding_sent").touch()
    return _create_synthetic_loop_env(mind_state_dir)


class EventWatcherSubprocessCapture:
    """Records calls to subprocess.run for assertion in event watcher tests."""

    def __init__(self, *, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._returncode = returncode
        self._stderr = stderr

    def run(self, cmd: list[str], **kwargs: Any) -> types.SimpleNamespace:
        self.calls.append((cmd, kwargs))
        return types.SimpleNamespace(returncode=self._returncode, stdout="", stderr=self._stderr)


@pytest.fixture()
def fake_mng_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake mng binary and set UV_TOOL_BIN_DIR to point to it."""
    bin_dir = tmp_path / "fake_bin"
    create_fake_mng_binary(bin_dir)
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_dir))
    return bin_dir


@pytest.fixture()
def mock_subprocess_success(monkeypatch: pytest.MonkeyPatch, fake_mng_binary: Path) -> EventWatcherSubprocessCapture:
    """Replace event_watcher's subprocess with a recording stub (returncode=0)."""
    capture = EventWatcherSubprocessCapture(returncode=0)
    mock_sp = types.SimpleNamespace(run=capture.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(event_watcher_module, "subprocess", mock_sp)
    return capture


@pytest.fixture()
def mock_subprocess_failure(monkeypatch: pytest.MonkeyPatch, fake_mng_binary: Path) -> EventWatcherSubprocessCapture:
    """Replace event_watcher's subprocess with a recording stub (returncode=1)."""
    capture = EventWatcherSubprocessCapture(returncode=1, stderr="send failed")
    mock_sp = types.SimpleNamespace(run=capture.run, TimeoutExpired=subprocess.TimeoutExpired)
    monkeypatch.setattr(event_watcher_module, "subprocess", mock_sp)
    return capture


def create_executable_script(directory: Path, name: str, content: str) -> str:
    """Create an executable script file and return its path as a string."""
    script = directory / name
    script.write_text(content)
    script.chmod(0o755)
    return str(script)
