"""Shared pytest fixtures for the mngr_antigravity package tests."""

from pathlib import Path

import pytest

from imbue.mngr_antigravity.antigravity_config import get_antigravity_oauth_token_path


@pytest.fixture
def isolated_home(tmp_path: Path) -> Path:
    """Seed the shared agy oauth token into the autouse-isolated HOME, and return it.

    ``$HOME`` is already redirected to ``tmp_path`` for every test by mngr's
    autouse ``setup_test_mngr_env`` fixture (pulled in via the package
    ``conftest``'s ``register_plugin_test_fixtures``), so this fixture only adds the
    antigravity-specific piece: the shared oauth token that ``provision`` reads
    and symlinks into each per-agent home. Tests that want a *clean* (no shared
    token) home simply don't request this fixture and use ``tmp_path`` directly.
    """
    token_path = get_antigravity_oauth_token_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("fake-oauth-token")
    return tmp_path
