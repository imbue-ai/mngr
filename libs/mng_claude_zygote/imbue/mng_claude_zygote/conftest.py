import os
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from imbue.mng.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mng.utils.testing import init_git_repo_with_config

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
        pass
    shutil.rmtree(tmux_tmpdir, ignore_errors=True)


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
    """

    def __init__(
        self,
        host_dir: Path = Path("/tmp/mng-test/host"),
        command_results: dict[str, StubCommandResult] | None = None,
        text_file_contents: dict[str, str] | None = None,
    ) -> None:
        self.host_dir = host_dir
        self.executed_commands: list[str] = []
        self.written_files: list[tuple[Path, bytes, str]] = []
        self.written_text_files: list[tuple[Path, str]] = []
        self._command_results = command_results or {}
        self._text_file_contents = text_file_contents or {}

    def execute_command(self, command: str, **kwargs: Any) -> StubCommandResult:
        self.executed_commands.append(command)
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
