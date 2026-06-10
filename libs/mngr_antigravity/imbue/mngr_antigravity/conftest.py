"""Shared pytest fixtures for the mngr_antigravity package tests."""

from collections.abc import Generator
from pathlib import Path

import pytest
from loguru import logger

from imbue.mngr_antigravity.antigravity_config import get_antigravity_oauth_token_path


@pytest.fixture
def log_warnings() -> Generator[list[str], None, None]:
    """Capture loguru warning messages for assertion in tests.

    Mirrors mngr's own ``log_warnings`` fixture (in ``libs/mngr``'s conftest),
    which is not on this package's fixture path. Tolerates handler removal during
    the test (e.g. ``setup_logging()`` calls ``logger.remove()``), so teardown
    never fails if the handler is already gone.
    """
    messages: list[str] = []
    handler_id = logger.add(lambda msg: messages.append(msg.record["message"]), level="WARNING", format="{message}")
    try:
        yield messages
    finally:
        try:
            logger.remove(handler_id)
        except ValueError:
            pass


@pytest.fixture
def isolated_home(tmp_path: Path) -> Path:
    """Seed the shared agy oauth token into the autouse-isolated HOME, and return it.

    ``$HOME`` is already redirected to ``tmp_path`` for every test by mngr's
    autouse ``setup_test_mngr_env`` fixture (pulled in via the package
    ``conftest``'s ``pytest_plugins``), so this fixture only adds the
    antigravity-specific piece: the shared oauth token that ``provision`` reads
    and symlinks into each per-agent home. Tests that want a *clean* (no shared
    token) home simply don't request this fixture and use ``tmp_path`` directly.
    """
    token_path = get_antigravity_oauth_token_path(tmp_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text("fake-oauth-token")
    return tmp_path
