import os
import sqlite3
import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.providers.ssh_host_setup import load_resource_script
from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr.utils.testing import init_git_repo_with_config
from imbue.mngr_llm.conftest import create_mind_conversations_table_in_test_db
from imbue.mngr_llm.provisioning import load_llm_resource
from imbue.mngr_mind.conftest import StubHost
from imbue.mngr_mind.conftest import isolate_tmux_server_impl
from imbue.mngr_mind.conftest import reset_loguru_impl

register_plugin_test_fixtures(globals())


@pytest.fixture(autouse=True)
def _reset_loguru() -> Generator[None, None, None]:
    yield from reset_loguru_impl()


@pytest.fixture(autouse=True)
def _isolate_tmux_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None, None, None]:
    yield from isolate_tmux_server_impl(monkeypatch)


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

    def _execute_command(self, command: str, **kwargs: Any) -> _ShellCommandResult:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, env=os.environ)
        return _ShellCommandResult(
            success=result.returncode == 0,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def execute_idempotent_command(self, command: str, **kwargs: Any) -> _ShellCommandResult:
        return self._execute_command(command, **kwargs)

    def execute_stateful_command(self, command: str, **kwargs: Any) -> _ShellCommandResult:
        return self._execute_command(command, **kwargs)

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

        mngr_log_path = commands_dir / "mngr_log.sh"
        mngr_log_path.write_text(load_resource_script("mngr_log.sh"))
        os.chmod(mngr_log_path, 0o755)
        self.messages_dir = self.agent_state_dir / "events" / "messages"
        self.messages_dir.mkdir(parents=True)

        self.llm_data_dir = self.agent_state_dir / "llm_data"
        self.llm_data_dir.mkdir(parents=True)
        self.llm_db_path = self.llm_data_dir / "logs.db"
        create_mind_conversations_table_in_test_db(self.llm_db_path)

        self.work_dir = temp_host_dir / "work"
        self.work_dir.mkdir(parents=True)

        self.env = os.environ.copy()
        self.env["MNGR_AGENT_STATE_DIR"] = str(self.agent_state_dir)
        self.env["MNGR_HOST_DIR"] = str(temp_host_dir)
        self.env["MNGR_AGENT_WORK_DIR"] = str(self.work_dir)
        self.env["LLM_USER_PATH"] = str(self.llm_data_dir)

    def set_default_model(self, model: str) -> None:
        """Set the chat model via MNGR_LLM_MODEL env var (and minds.toml for backward compat)."""
        self.env["MNGR_LLM_MODEL"] = model
        (self.work_dir / "minds.toml").write_text(f'[chat]\nmodel = "{model}"\n')

    def run(self, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
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


def parse_chat_output(stdout: str) -> dict[str, str]:
    """Parse key=value pairs from chat.sh output.

    Returns a dict mapping keys to values. Lines that are not in
    key=value format are ignored.
    """
    result: dict[str, str] = {}
    for line in stdout.strip().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def create_mind_conversations_table_only(db_path: Path) -> None:
    """Create only the mind_conversations table (not llm's conversations table).

    Used in tests that need ``llm inject`` to run its own migrations.
    The standard ``create_mind_conversations_table_in_test_db`` creates both
    tables, which conflicts with llm's migration system.
    """
    from imbue.mngr_llm.provisioning import MIND_CONVERSATIONS_TABLE_SQL

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(MIND_CONVERSATIONS_TABLE_SQL)
        conn.commit()


def assert_conversation_exists_in_db(db_path: Path, conversation_id: str) -> None:
    """Assert that a conversation record exists in the mind_conversations table."""
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT conversation_id FROM mind_conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchall()
    assert len(rows) == 1, f"Expected conversation {conversation_id} in DB, found {len(rows)} rows"
