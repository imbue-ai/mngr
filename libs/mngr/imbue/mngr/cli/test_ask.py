"""Acceptance test for the mngr ask command.

Verifies that `mngr ask` can send a query to a real Claude agent and
receive a non-empty response. Requires Claude Code CLI and API credentials.

Run with:
    pytest -m acceptance libs/mngr/imbue/mngr/cli/test_ask.py --timeout=120
"""

import json
from pathlib import Path

import pytest

from imbue.mngr.utils.testing import is_claude_installed
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr.utils.testing import setup_claude_trust_config_for_subprocess

pytestmark = pytest.mark.skipif(not is_claude_installed(), reason="Claude Code CLI is not installed")


@pytest.mark.acceptance
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_ask_simple_query(temp_git_repo: Path) -> None:
    """mngr ask should return a non-empty response from Claude."""
    env = setup_claude_trust_config_for_subprocess([temp_git_repo])
    result = run_mngr_subprocess(
        "ask",
        "just say hi",
        "--format",
        "json",
        "--disable-plugin",
        "modal",
        env=env,
        cwd=temp_git_repo,
    )
    assert result.returncode == 0, f"mngr ask failed: {result.stderr}"
    parsed = json.loads(result.stdout)
    assert len(parsed["response"].strip()) > 0, f"Expected non-empty response, got: {parsed['response']!r}"
