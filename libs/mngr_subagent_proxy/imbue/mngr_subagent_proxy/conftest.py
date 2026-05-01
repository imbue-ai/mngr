"""Shared test fixtures for mngr_subagent_proxy unit tests.

Both ``hooks_test.py`` and ``hooks/deny_test.py`` need to clear the same
set of subagent-proxy env vars so individual tests can set only what they
need. Per the project style guide (CLAUDE.md), shared test fixtures
belong in ``conftest.py`` rather than being duplicated in each test file.
"""

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
