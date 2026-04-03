"""Tests for the mngr ask command with the Claude plugin."""

import json

import pytest

from imbue.mngr.e2e.conftest import E2eSession
from imbue.skitwright.expect import expect

_ASK_TIMEOUT = 120.0


def _skip_if_claude_not_authenticated(e2e: E2eSession) -> None:
    """Skip the test if Claude is not authenticated in the e2e environment."""
    result = e2e.run("claude auth status", comment="Check Claude auth", timeout=10)
    if result.exit_code != 0:
        pytest.skip("Claude Code is not available in e2e environment")
    try:
        data = json.loads(result.stdout)
        if not data.get("loggedIn", False):
            pytest.skip("Claude Code is not authenticated in e2e environment")
    except (json.JSONDecodeError, ValueError):
        pytest.skip("Could not parse Claude auth status")


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(120)
def test_ask_simple_query(e2e: E2eSession) -> None:
    _skip_if_claude_not_authenticated(e2e)
    result = e2e.run(
        'mngr ask "just say hi" --format json',
        comment="Ask Claude a simple question via mngr ask",
        timeout=_ASK_TIMEOUT,
    )
    expect(result).to_succeed()
    parsed = json.loads(result.stdout)
    assert len(parsed["response"].strip()) > 0, f"Expected non-empty response, got: {parsed['response']!r}"
