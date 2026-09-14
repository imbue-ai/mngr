"""Shared pytest fixtures for the mngr_codex package tests."""

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from websockets.sync.server import ServerConnection
from websockets.sync.server import unix_serve

from imbue.mngr_codex.codex_config import get_codex_auth_path


@pytest.fixture
def codex_large_frame_socket() -> Iterator[Path]:
    def handle(connection: ServerConnection) -> None:
        for message in connection:
            connection.send("x" * (2 * 1024 * 1024) if message == "history" else "ready")

    # Keep the Unix socket path short enough for macOS as well as Linux.
    with TemporaryDirectory(prefix="codex-ws-", dir="/tmp") as directory:
        socket_path = Path(directory) / "socket"
        with unix_serve(handle, path=str(socket_path), compression=None) as server:
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(server.serve_forever)
                try:
                    yield socket_path
                finally:
                    server.shutdown()


@pytest.fixture
def isolated_codex_home(tmp_path: Path) -> Path:
    """Seed the shared codex auth.json into the autouse-isolated HOME, and return it.

    ``$HOME`` is already redirected to ``tmp_path`` for every test by mngr's
    autouse ``setup_test_mngr_env`` fixture (pulled in via the package
    ``conftest``'s ``register_plugin_test_fixtures``), so this fixture only adds
    the codex-specific piece: the shared ``auth.json`` that ``provision`` reads
    and symlinks into each per-agent ``CODEX_HOME``. The user's real codex home
    is ``~/.codex`` (``tmp_path/".codex"``), and ``get_codex_auth_path`` returns
    ``<CODEX_HOME>/auth.json`` for that root. Tests that want a *clean* (no shared
    auth) home simply don't request this fixture and use ``tmp_path`` directly.
    """
    auth_path = get_codex_auth_path(tmp_path / ".codex")
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "tokens": {"access_token": "fake"},
                "last_refresh": "2026-01-01T00:00:00Z",
            }
        )
    )
    return tmp_path
