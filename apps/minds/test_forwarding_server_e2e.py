"""End-to-end test for the minds forwarding server.

Creates an agent from the forever-claude-template repo via the forwarding
server API, verifies it starts and its web server is accessible through
the forwarding server proxy, then waits for a signal file before tearing down.

Run from the repo root:
    just test apps/minds/test_forwarding_server_e2e.py::test_create_agent_e2e

The test waits for /tmp/minds-e2e-done to exist before tearing down.
Create this file to signal the test to finish:
    touch /tmp/minds-e2e-done
"""

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import dotenv
import httpx
import pytest
import uvicorn
from loguru import logger

from imbue.minds.config.data_types import MindPaths
from imbue.minds.forwarding_server.agent_creator import AgentCreator
from imbue.minds.forwarding_server.app import create_forwarding_server
from imbue.minds.forwarding_server.auth import FileAuthStore
from imbue.minds.forwarding_server.backend_resolver import MngrCliBackendResolver
from imbue.minds.forwarding_server.backend_resolver import MngrStreamManager
from imbue.minds.primitives import OneTimeCode

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_REPO = Path.home() / "project" / "forever-claude-template"
_SIGNAL_FILE = Path("/tmp/minds-e2e-done")
_AGENT_NAME = "forever"


def _configure_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG",
        format="{time:HH:mm:ss.SSS} | {level:<7} | {name}:{function}:{line} - {message}",
    )


def _load_env() -> None:
    env_file = _REPO_ROOT / ".env"
    if env_file.exists():
        dotenv.load_dotenv(env_file)
    test_env_file = _REPO_ROOT / ".test_env"
    if test_env_file.exists():
        dotenv.load_dotenv(test_env_file)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _destroy_agent(agent_name: str) -> None:
    try:
        subprocess.run(
            ["uv", "run", "mngr", "destroy", agent_name, "--force"],
            input="y\n",
            capture_output=True,
            timeout=30,
            text=True,
            cwd=_REPO_ROOT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


class ForwardingServerFixture:

    def __init__(self, tmp_dir: Path) -> None:
        self.host = "127.0.0.1"
        self.port = _find_free_port()
        self.code = OneTimeCode("test-code-for-e2e-12345")
        self.tmp_dir = tmp_dir
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._stream_manager: MngrStreamManager | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        paths = MindPaths(data_dir=self.tmp_dir)
        auth_store = FileAuthStore(data_directory=paths.auth_dir)
        auth_store.add_one_time_code(code=self.code)

        backend_resolver = MngrCliBackendResolver()
        self._stream_manager = MngrStreamManager(resolver=backend_resolver)
        agent_creator = AgentCreator(paths=paths)

        app = create_forwarding_server(
            auth_store=auth_store,
            backend_resolver=backend_resolver,
            http_client=None,
            agent_creator=agent_creator,
        )

        self._stream_manager.start()

        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)

        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

        for _ in range(50):
            try:
                with socket.create_connection((self.host, self.port), timeout=0.1):
                    logger.info("Forwarding server started on {}:{}", self.host, self.port)
                    return
            except (ConnectionRefusedError, OSError):
                threading.Event().wait(0.1)

        pytest.fail("Forwarding server did not start within 5 seconds")

    def stop(self) -> None:
        if self._stream_manager is not None:
            self._stream_manager.stop()
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)


def _create_agent_with_retry(client: httpx.Client, max_attempts: int = 2) -> str:
    """Create an agent via API, retrying on failure (e.g. provisioning timeout)."""
    for attempt in range(max_attempts):
        logger.info("Creating agent (attempt {}/{})", attempt + 1, max_attempts)
        resp = client.post(
            "/api/create-agent",
            json={
                "git_url": str(_TEMPLATE_REPO),
                "agent_name": _AGENT_NAME,
                "launch_mode": "LOCAL",
            },
        )
        assert resp.status_code == 200, f"Create API failed: {resp.status_code} {resp.text}"
        agent_id = resp.json()["agent_id"]

        for i in range(300):
            resp = client.get(f"/api/create-agent/{agent_id}/status")
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status")
                if status == "DONE":
                    logger.info("Agent created in {} seconds", i)
                    return agent_id
                if status == "FAILED":
                    error = data.get("error", "unknown")
                    logger.warning("Creation failed: {}", error)
                    if attempt < max_attempts - 1:
                        logger.info("Retrying...")
                        _destroy_agent(_AGENT_NAME)
                        break
                    pytest.fail(f"Agent creation failed after {max_attempts} attempts: {error}")
            threading.Event().wait(1)
        else:
            if attempt < max_attempts - 1:
                logger.warning("Creation timed out, retrying...")
                _destroy_agent(_AGENT_NAME)
                continue
            pytest.fail("Agent creation timed out after {max_attempts} attempts")

    pytest.fail("Unreachable")


@pytest.mark.release
@pytest.mark.timeout(600)
def test_create_agent_e2e(tmp_path: Path) -> None:
    """Create an agent and verify its web server is accessible through the forwarding server."""
    _configure_logging()
    _load_env()
    os.environ["MIND_NAME"] = _AGENT_NAME

    _SIGNAL_FILE.unlink(missing_ok=True)
    _destroy_agent(_AGENT_NAME)

    server = ForwardingServerFixture(tmp_path)
    server.start()

    client = httpx.Client(
        base_url=server.base_url,
        cookies={"mind_session": "skip"},
        timeout=15.0,
    )
    os.environ["SKIP_AUTH"] = "1"

    try:
        # Create agent via API (with retry for transient provisioning failures)
        agent_id = _create_agent_with_retry(client, max_attempts=2)
        logger.info("Agent ready: {}", agent_id)

        # Wait for the forwarding server to discover the agent's servers
        logger.info("Waiting for server discovery...")
        for i in range(60):
            resp = client.get(f"/agents/{agent_id}/servers/")
            if resp.status_code == 200 and "web" in resp.text:
                logger.info("Web server discovered after {} seconds", i)
                break
            threading.Event().wait(1)
        else:
            pytest.fail("Web server not discovered within 60 seconds")

        # Verify the web server is accessible through the proxy
        resp = client.get(f"/agents/{agent_id}/web/", follow_redirects=True)
        assert resp.status_code == 200, f"Web proxy failed: {resp.status_code}"
        logger.info("Web server accessible via proxy")

        logger.info("Server: {}", server.base_url)
        logger.info("Agent servers: {}/agents/{}/servers/", server.base_url, agent_id)
        logger.info("Waiting for signal file: {}", _SIGNAL_FILE)
        logger.info("Create it to finish: touch {}", _SIGNAL_FILE)

        while not _SIGNAL_FILE.exists():
            threading.Event().wait(1)

        logger.info("Signal received, tearing down...")

    finally:
        client.close()
        _destroy_agent(_AGENT_NAME)
        server.stop()
        _SIGNAL_FILE.unlink(missing_ok=True)
