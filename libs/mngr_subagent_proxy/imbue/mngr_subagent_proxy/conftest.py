"""Shared test fixtures for mngr_subagent_proxy unit tests.

Both ``hooks_test.py`` and ``hooks/deny_test.py`` need to clear the same
set of subagent-proxy env vars and stand up a fresh ``state_dir`` under
``tmp_path``. Per the project style guide (CLAUDE.md), shared test
fixtures belong in ``conftest.py`` rather than being duplicated in each
test file.
"""

from pathlib import Path

import pytest


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Clear subagent-proxy env vars so individual tests set only what they need.

    Covers the union of vars referenced by any subagent-proxy unit test:
    ``MNGR_AGENT_STATE_DIR`` and ``MNGR_AGENT_NAME`` (set by both
    spawn/deny entry points), ``MNGR_SUBAGENT_DEPTH`` /
    ``MNGR_MAX_SUBAGENT_DEPTH`` (depth-limit guard), and
    ``MNGR_SUBAGENT_REAP_BACKGROUND`` (reap hook background-worker switch).
    """
    for name in (
        "MNGR_AGENT_STATE_DIR",
        "MNGR_AGENT_NAME",
        "MNGR_SUBAGENT_DEPTH",
        "MNGR_MAX_SUBAGENT_DEPTH",
        "MNGR_SUBAGENT_REAP_BACKGROUND",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """A pre-created ``$MNGR_AGENT_STATE_DIR`` under the per-test ``tmp_path``.

    Hook entry points read ``MNGR_AGENT_STATE_DIR`` from the env and write
    sidefiles into it; tests need a real directory on disk. Co-located
    under ``tmp_path`` so pytest's per-test temp-dir cleanup handles
    disposal.
    """
    path = tmp_path / "state"
    path.mkdir()
    return path
