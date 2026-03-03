import os
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from imbue.mng.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mng.utils.testing import init_git_repo_with_config
from imbue.mng_claude_zygote.provisioning import load_zygote_resource

register_plugin_test_fixtures(globals())


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
    """Test double that executes commands via subprocess on the local filesystem.

    Unlike StubHost (which records commands without executing them), this host
    actually runs commands via subprocess.run(shell=True). Used by integration
    tests that need real filesystem side effects (symlinks, directories, etc.).
    """

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
    """Environment for running the chat.sh script in tests.

    Provides the script path, agent state directory, and environment variables
    needed to invoke chat.sh in a subprocess.
    """

    def __init__(self, temp_host_dir: Path) -> None:
        self.chat_script = temp_host_dir / "commands" / "chat.sh"
        self.chat_script.parent.mkdir(parents=True)
        self.chat_script.write_text(load_zygote_resource("chat.sh"))
        os.chmod(self.chat_script, 0o755)

        self.agent_state_dir = temp_host_dir / "agents" / "test-agent"
        self.conversations_dir = self.agent_state_dir / "logs" / "conversations"
        self.conversations_dir.mkdir(parents=True)
        self.messages_dir = self.agent_state_dir / "logs" / "messages"
        self.messages_dir.mkdir(parents=True)

        self.env = os.environ.copy()
        self.env["MNG_AGENT_STATE_DIR"] = str(self.agent_state_dir)
        self.env["MNG_HOST_DIR"] = str(temp_host_dir)

    def set_default_model(self, model: str) -> None:
        """Write the chat model to settings.toml."""
        (self.agent_state_dir / "settings.toml").write_text(f'[chat]\nmodel = "{model}"\n')

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
    """Concrete test double for OnlineHostInterface that records operations.

    Records all execute_command calls and write_file/write_text_file calls
    for assertion in tests. Supports optional text_file_contents for
    read_text_file stubbing.

    If execute_mkdir is True, 'mkdir -p' commands will actually create
    directories on the local filesystem in addition to being recorded.
    """

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
        # For `cd <path> && pwd`, return the path as stdout
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
