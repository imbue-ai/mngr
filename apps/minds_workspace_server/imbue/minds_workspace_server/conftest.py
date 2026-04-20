import os
import subprocess
from collections.abc import Callable
from collections.abc import Generator
from pathlib import Path

import pytest
from playwright.sync_api import Browser

from imbue.minds_workspace_server.agent_manager import AgentManager
from imbue.minds_workspace_server.ws_broadcaster import WebSocketBroadcaster


@pytest.fixture
def browser(launch_browser: Callable[[], Browser]) -> Generator[Browser, None, None]:
    """Override pytest-playwright's session-scoped browser to function scope.

    The upstream `browser` fixture is session-scoped; its teardown runs at
    pytest session exit, after mngr's autouse `session_cleanup` fixture
    (libs/mngr/imbue/mngr/conftest.py) has already checked for leaked child
    processes. In offload release batches that mix workspace-server e2e
    tests with other mngr tests, the still-alive chrome-headless-shell
    processes trip `session_cleanup`'s leak assertion and cascade into
    teardown errors for every sibling test in the batch.

    Making the browser per-test guarantees each test's chromium is closed
    synchronously in its own teardown, before any session-level check
    runs. The per-launch cost (~1s) is negligible relative to the total
    test time, and avoids a brittle race with external fixture ordering.
    """
    b = launch_browser()
    yield b
    b.close()


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
