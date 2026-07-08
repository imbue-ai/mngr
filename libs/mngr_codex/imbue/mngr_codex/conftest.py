"""Shared pytest fixtures for the mngr_codex package tests."""

import json
from pathlib import Path

import pytest

from imbue.mngr_codex.codex_config import get_codex_auth_path


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
