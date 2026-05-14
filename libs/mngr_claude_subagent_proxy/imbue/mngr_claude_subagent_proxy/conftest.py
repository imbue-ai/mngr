"""Shared test fixtures for mngr_claude_subagent_proxy unit tests.

Both ``hooks_test.py`` and ``hooks/deny_test.py`` need to clear the same
set of subagent-proxy env vars and stand up a fresh ``state_dir`` under
``tmp_path``. Per the project style guide (CLAUDE.md), shared test
fixtures belong in ``conftest.py`` rather than being duplicated in each
test file.
"""

import json
from pathlib import Path
from typing import Callable

import pytest


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """Clear subagent-proxy env vars so individual tests set only what they need.

    Covers the union of vars referenced by any subagent-proxy unit test:
    ``MNGR_AGENT_STATE_DIR`` and ``MNGR_AGENT_NAME`` (set by both
    spawn/deny entry points), ``MNGR_AGENT_ID`` (reap hook's
    label-driven destroy filter), ``MNGR_SUBAGENT_DEPTH`` /
    ``MNGR_MAX_SUBAGENT_DEPTH`` (depth-limit guard), and
    ``MNGR_SUBAGENT_REAP_BACKGROUND`` (reap hook background-worker switch).

    Also reroutes ``$HOME`` into a per-test ``tmp_path`` so the
    typed-subagent agent-definition resolver (which goes through
    ``get_user_claude_config_dir()``: ``$ORIGINAL_CLAUDE_CONFIG_DIR``
    then ``$CLAUDE_CONFIG_DIR`` then ``Path.home() / ".claude"``)
    cannot accidentally read the developer's real ``~/.claude/agents/``
    or installed marketplace plugins. The autouse
    ``setup_test_mngr_env`` fixture (via ``isolate_home``) already
    clears the two config-dir vars, so overriding ``HOME`` here is
    enough to redirect the lookup. Tests that exercise typed-subagent
    paths can drop files under the fake home; tests that don't get a
    deterministic empty home that resolves nothing.
    """
    for name in (
        "MNGR_AGENT_STATE_DIR",
        "MNGR_AGENT_NAME",
        "MNGR_AGENT_ID",
        "MNGR_SUBAGENT_DEPTH",
        "MNGR_MAX_SUBAGENT_DEPTH",
        "MNGR_SUBAGENT_REAP_BACKGROUND",
    ):
        monkeypatch.delenv(name, raising=False)
    fake_home = tmp_path / "fake_home_default"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
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


@pytest.fixture
def hook_env(clean_env: pytest.MonkeyPatch, state_dir: Path) -> pytest.MonkeyPatch:
    """``clean_env`` plus ``MNGR_AGENT_STATE_DIR`` / ``MNGR_AGENT_NAME`` seeded.

    Both PROXY (``hooks/spawn.py``) and DENY (``hooks/deny.py``) entry
    points require these two env vars on every non-pass-through path.
    Returns the same ``pytest.MonkeyPatch`` so tests can compose
    additional ``setenv`` / ``delenv`` calls (e.g. depth-limit env vars)
    on top of the seeded baseline.
    """
    clean_env.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))
    clean_env.setenv("MNGR_AGENT_NAME", "parent-agent")
    return clean_env


@pytest.fixture
def write_unguarded_orchestrator_hooks() -> Callable[[Path], None]:
    """Return a writer that drops a hooks.json mimicking an un-guarded
    stop-hook orchestrator plugin marketplace entry.

    Shared by the Stop-hook guard tests in ``hooks_test.py`` and
    ``hooks/guard_stop_hooks_test.py``; lives here so both files use the
    same definition rather than maintaining byte-identical copies.
    """

    def _write(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "timeout": 900,
                                        "command": "${CLAUDE_PLUGIN_ROOT}/scripts/stop_hook_orchestrator.sh",
                                    }
                                ]
                            }
                        ]
                    }
                }
            )
            + "\n"
        )

    return _write
