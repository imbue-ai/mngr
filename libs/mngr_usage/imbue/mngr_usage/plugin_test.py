"""Unit tests for mngr_usage.plugin (hookimpl behavior)."""

from __future__ import annotations

from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_usage.plugin import _extract_user_statusline_command
from imbue.mngr_usage.plugin import _format_statusline_command
from imbue.mngr_usage.plugin import claude_extra_per_agent_settings


def test_extract_user_statusline_command_picks_up_existing() -> None:
    settings = {"statusLine": {"type": "command", "command": "/path/to/caveman.sh"}}
    assert _extract_user_statusline_command(settings) == "/path/to/caveman.sh"


def test_extract_user_statusline_command_handles_missing() -> None:
    assert _extract_user_statusline_command({}) is None
    assert _extract_user_statusline_command({"statusLine": {}}) is None
    assert _extract_user_statusline_command({"statusLine": {"command": "  "}}) is None
    assert _extract_user_statusline_command({"statusLine": "not a dict"}) is None


def test_format_statusline_command_quotes_state_dir(tmp_path: Path) -> None:
    state_dir = tmp_path / "state with spaces"
    cmd = _format_statusline_command(state_dir)
    assert "MNGR_AGENT_STATE_DIR=" in cmd
    assert "claude_statusline.sh" in cmd


def test_claude_extra_per_agent_settings_wraps_existing_command(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    state_dir = tmp_path / "agent_state"
    contribution = claude_extra_per_agent_settings(
        mngr_ctx=temp_mngr_ctx,
        source_settings={"statusLine": {"command": "/caveman.sh"}},
        agent_state_dir=state_dir,
    )
    assert contribution is not None
    assert contribution.statusline_command is not None
    assert "claude_statusline.sh" in contribution.statusline_command
    assert contribution.env["MNGR_USER_STATUSLINE_CMD"] == "/caveman.sh"
    assert contribution.env["MNGR_RATE_LIMITS_WRITER"].endswith("claude_rate_limits_writer.sh")
    assert contribution.env["MNGR_RATE_LIMITS_CACHE"].endswith("claude_rate_limits.json")
    assert "claude_statusline.sh" in contribution.resource_scripts
    assert "claude_rate_limits_writer.sh" in contribution.resource_scripts


def test_claude_extra_per_agent_settings_handles_no_existing_command(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    state_dir = tmp_path / "agent_state"
    contribution = claude_extra_per_agent_settings(
        mngr_ctx=temp_mngr_ctx,
        source_settings={},
        agent_state_dir=state_dir,
    )
    assert contribution is not None
    assert "MNGR_USER_STATUSLINE_CMD" not in contribution.env
