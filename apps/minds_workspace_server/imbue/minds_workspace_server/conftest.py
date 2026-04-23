import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger as loguru_logger

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
def false_binary() -> str:
    """Cross-platform path to a binary that exits immediately with failure.

    Used by tests that exercise the observe watchdog's error path without
    relying on a real mngr installation.
    """
    path = shutil.which("false")
    assert path is not None, "Could not find 'false' binary on this system"
    return path


@pytest.fixture
def loguru_records() -> Iterator[list[str]]:
    """Capture loguru log messages as plain strings for test assertions.

    Each entry in the yielded list is a ``"<LEVEL> <message>"`` line, so tests
    can filter on both level and text without wiring up loguru into pytest's
    stdlib-oriented ``caplog``.
    """
    messages: list[str] = []
    handler_id = loguru_logger.add(
        lambda msg: messages.append(f"{msg.record['level'].name} {msg.record['message']}"),
        level="DEBUG",
        format="{message}",
    )
    try:
        yield messages
    finally:
        loguru_logger.remove(handler_id)


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
