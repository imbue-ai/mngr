"""Acceptance test for the mngr ask command.

Verifies that `mngr ask` can send a query to a real Claude agent and
receive a non-empty response. Requires Claude Code CLI and API credentials.

Run with:
    pytest -m acceptance libs/mngr/imbue/mngr/cli/test_ask.py --timeout=120
"""

import json
import os
from pathlib import Path

import pytest

from imbue.mngr.utils.testing import is_claude_installed
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr.utils.testing import setup_claude_trust_config_for_subprocess

pytestmark = pytest.mark.skipif(not is_claude_installed(), reason="Claude Code CLI is not installed")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_ask_simple_query(temp_git_repo: Path) -> None:
    """mngr ask should return a non-empty response from Claude."""
    # Fail fast with a clear signal if the env propagation chain dropped the
    # key before the test process ran: justfile --env → offload sandbox →
    # pytest process. A previous offload-sandbox diagnostic showed the tmux
    # pane's env was missing ANTHROPIC_API_KEY entirely, so surface whether
    # the loss is upstream of pytest (here) vs inside mngr's tmux launch.
    outer_key = os.environ.get("ANTHROPIC_API_KEY", "")
    assert outer_key, (
        "ANTHROPIC_API_KEY is not set in the test process env. "
        "Something in the justfile --env → offload sandbox → pytest chain dropped it."
    )
    env = setup_claude_trust_config_for_subprocess([temp_git_repo])
    assert env.get("ANTHROPIC_API_KEY"), (
        "ANTHROPIC_API_KEY was in os.environ but not in the subprocess env dict "
        "returned by setup_claude_trust_config_for_subprocess."
    )
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
