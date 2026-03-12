import os
import shutil
import subprocess
import tempfile
import types
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from imbue.mng.providers.ssh_host_setup import load_resource_script
from imbue.mng.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mng.utils.testing import init_git_repo_with_config
from imbue.mng_claude_mind.resources import event_watcher as event_watcher_module
from imbue.mng_llm.conftest import (
    create_mind_conversations_table_in_test_db as create_mind_conversations_table_in_test_db,
)
from imbue.mng_llm.conftest import create_test_llm_db as create_test_llm_db
from imbue.mng_llm.conftest import web_server_test_server as web_server_test_server
from imbue.mng_llm.conftest import write_conversation_to_db as write_conversation_to_db
from imbue.mng_llm.conftest import write_minds_settings_toml as write_minds_settings_toml
from imbue.mng_llm.provisioning import MIND_CONVERSATIONS_TABLE_SQL as MIND_CONVERSATIONS_TABLE_SQL
from imbue.mng_llm.provisioning import load_llm_resource

register_plugin_test_fixtures(globals())


@pytest.fixture(autouse=True)
def _reset_loguru() -> Generator[None, None, None]:
    """Reset loguru handlers before and after each test to prevent handler leakage."""
    logger.remove()
    yield
    logger.remove()


@pytest.fixture(autouse=True)
def _isolate_tmux_server(
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


class _ShellCommandResult:
    """Result of a shell command execution, matching the interface expected by provisioning code."""

    def __init__(self, *, success: bool, stdout: str, stderr: str) -> None:
        self.success = success
        self.stdout = stdout
        self.stderr = stderr


class LocalShellHost:
    """Test double that executes commands via subprocess on the local filesystem."""

    def __init__(self, host_dir: Path) -> None:
        self.host_dir = host_dir

    def execute_command(self, command: str, **kwargs: Any) -> _ShellCommandResult:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, env=os.environ)
        return _ShellCommandResult(
            success=result.returncode == 0,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def read_text_file(self, path: Path) -> str:
        return path.read_text()

    def write_text_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def write_file(self, path: Path, content: bytes, mode: str = "0644") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.chmod(path, int(mode, 8))


@pytest.fixture()
def local_shell_host(temp_host_dir: Path) -> LocalShellHost:
    """Provide a LocalShellHost backed by the temp host directory."""
    return LocalShellHost(host_dir=temp_host_dir)


class ChatScriptEnv:
    """Environment for running the chat.sh script in tests."""

    def __init__(self, temp_host_dir: Path) -> None:
        self.agent_state_dir = temp_host_dir / "agents" / "test-agent"

        commands_dir = self.agent_state_dir / "commands"
        commands_dir.mkdir(parents=True)

        self.chat_script = commands_dir / "chat.sh"
        self.chat_script.write_text(load_llm_resource("chat.sh"))
        os.chmod(self.chat_script, 0o755)

        mng_log_path = commands_dir / "mng_log.sh"
        mng_log_path.write_text(load_resource_script("mng_log.sh"))
        os.chmod(mng_log_path, 0o755)
        self.messages_dir = self.agent_state_dir / "events" / "messages"
        self.messages_dir.mkdir(parents=True)

        self.llm_data_dir = self.agent_state_dir / "llm_data"
        self.llm_data_dir.mkdir(parents=True)
        self.llm_db_path = self.llm_data_dir / "logs.db"
        create_mind_conversations_table_in_test_db(self.llm_db_path)

        self.work_dir = temp_host_dir / "work"
        self.work_dir.mkdir(parents=True)

        self.env = os.environ.copy()
        self.env["MNG_AGENT_STATE_DIR"] = str(self.agent_state_dir)
        self.env["MNG_HOST_DIR"] = str(temp_host_dir)
        self.env["MNG_AGENT_WORK_DIR"] = str(self.work_dir)
        self.env["LLM_USER_PATH"] = str(self.llm_data_dir)

    def set_default_model(self, model: str) -> None:
        """Write the chat model to minds.toml in the work dir."""
        (self.work_dir / "minds.toml").write_text(f'[chat]\nmodel = "{model}"\n')

    def run(self, *args: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
        """Run chat.sh with the given arguments."""
        return subprocess.run(
            [str(self.chat_script), *args],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=timeout,
        )


@pytest.fixture()
def chat_env(temp_host_dir: Path) -> ChatScriptEnv:
    """Provide a ChatScriptEnv for testing chat.sh."""
    return ChatScriptEnv(temp_host_dir)


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


@pytest.fixture()
def fake_mng_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake mng binary and set UV_TOOL_BIN_DIR to point to it."""
    bin_dir = tmp_path / "fake_bin"
    create_fake_mng_binary(bin_dir)
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_dir))
    return bin_dir


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


def assert_conversation_exists_in_db(db_path: Path, conversation_id: str) -> None:
    """Assert that a conversation record exists in the mind_conversations table."""
    import sqlite3

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT conversation_id FROM mind_conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchall()
    assert len(rows) == 1, f"Expected conversation {conversation_id} in DB, found {len(rows)} rows"
