import os
import subprocess
from pathlib import Path

import pytest

from imbue.minds_workspace_server.agent_manager import AgentManager
from imbue.minds_workspace_server.ws_broadcaster import WebSocketBroadcaster


@pytest.fixture
def broadcaster() -> WebSocketBroadcaster:
    return WebSocketBroadcaster()


@pytest.fixture
def agent_manager(broadcaster: WebSocketBroadcaster, monkeypatch: pytest.MonkeyPatch) -> AgentManager:
    """Create an AgentManager without starting the observe subprocess."""
    monkeypatch.setenv("MNGR_AGENT_ID", "test-agent-id")
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", "/tmp/test-work")
    return AgentManager.build(broadcaster)


@pytest.fixture
def git_work_dir(tmp_path: Path) -> Path:
    """Create a minimal git repository for tests that need a real git work directory."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        },
    )
    return tmp_path
