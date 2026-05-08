"""Unit tests for mngr_claude_usage.plugin (hookimpl behavior)."""

from __future__ import annotations

from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_claude_usage.plugin import _extract_user_statusline_command
from imbue.mngr_claude_usage.plugin import _format_statusline_command
from imbue.mngr_claude_usage.plugin import claude_extra_per_agent_settings


def test_extract_user_statusline_command_picks_up_existing() -> None:
    settings = {"statusLine": {"type": "command", "command": "/path/to/caveman.sh"}}
    assert _extract_user_statusline_command(settings, own_shim_path="/different/shim.sh") == "/path/to/caveman.sh"


def test_extract_user_statusline_command_handles_missing() -> None:
    assert _extract_user_statusline_command({}, own_shim_path="/x") is None
    assert _extract_user_statusline_command({"statusLine": {}}, own_shim_path="/x") is None
    assert _extract_user_statusline_command({"statusLine": {"command": "  "}}, own_shim_path="/x") is None
    assert _extract_user_statusline_command({"statusLine": "not a dict"}, own_shim_path="/x") is None


def test_extract_user_statusline_command_skips_self_recursion() -> None:
    """Re-provisioning sees the previously-installed shim path as the effective
    statusLine command. We must skip it instead of capturing it as
    MNGR_USER_STATUSLINE_CMD, otherwise the shim chains to itself."""
    own_shim = "/Users/ev/.mngr/agents/agent-XXX/commands/claude_statusline.sh"
    settings = {"statusLine": {"type": "command", "command": own_shim}}
    assert _extract_user_statusline_command(settings, own_shim_path=own_shim) is None


def test_format_statusline_command_quotes_state_dir(tmp_path: Path) -> None:
    state_dir = tmp_path / "state with spaces"
    cmd = _format_statusline_command(state_dir)
    assert "'" in cmd or '"' in cmd
    assert "claude_statusline.sh" in cmd
    # No leftover env-var prefix; env is set via plugin_env_vars.json.
    assert "=" not in cmd


def test_claude_extra_per_agent_settings_wraps_existing_command(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    state_dir = tmp_path / "agent_state"
    work_dir = tmp_path / "work"
    contribution = claude_extra_per_agent_settings(
        mngr_ctx=temp_mngr_ctx,
        source_settings={"statusLine": {"command": "/caveman.sh"}},
        agent_state_dir=state_dir,
        work_dir=work_dir,
        is_local=True,
    )
    assert contribution is not None
    assert contribution.statusline_command is not None
    assert "claude_statusline.sh" in contribution.statusline_command
    assert contribution.env["MNGR_USER_STATUSLINE_CMD"] == "/caveman.sh"
    assert contribution.env["MNGR_RATE_LIMITS_WRITER"].endswith("claude_rate_limits_writer.sh")
    # No more MNGR_RATE_LIMITS_CACHE / MNGR_PROFILE_DIR -- we write events to
    # $MNGR_AGENT_STATE_DIR/events/claude/rate_limits/events.jsonl, which the
    # writer derives from MNGR_AGENT_STATE_DIR (set by mngr core).
    assert "MNGR_RATE_LIMITS_CACHE" not in contribution.env
    assert "MNGR_PROFILE_DIR" not in contribution.env
    assert "claude_statusline.sh" in contribution.resource_scripts
    assert "claude_rate_limits_writer.sh" in contribution.resource_scripts


def test_claude_extra_per_agent_settings_handles_no_existing_command(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    state_dir = tmp_path / "agent_state"
    work_dir = tmp_path / "work"
    contribution = claude_extra_per_agent_settings(
        mngr_ctx=temp_mngr_ctx,
        source_settings={},
        agent_state_dir=state_dir,
        work_dir=work_dir,
        is_local=True,
    )
    assert contribution is not None
    assert "MNGR_USER_STATUSLINE_CMD" not in contribution.env


def test_claude_extra_per_agent_settings_skips_remote_hosts(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """For non-local hosts the events file lives under the remote agent's
    filesystem, which `mngr usage` (run locally) never reads."""
    state_dir = tmp_path / "agent_state"
    work_dir = tmp_path / "work"
    contribution = claude_extra_per_agent_settings(
        mngr_ctx=temp_mngr_ctx,
        source_settings={"statusLine": {"command": "/caveman.sh"}},
        agent_state_dir=state_dir,
        work_dir=work_dir,
        is_local=False,
    )
    assert contribution is None


def test_claude_extra_per_agent_settings_skips_self_referenced_shim(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """Re-provision case: source_settings already has our shim installed as
    statusLine. The hookimpl must not capture that as MNGR_USER_STATUSLINE_CMD."""
    state_dir = tmp_path / "agent_state"
    work_dir = tmp_path / "work"
    own_shim = _format_statusline_command(state_dir)
    contribution = claude_extra_per_agent_settings(
        mngr_ctx=temp_mngr_ctx,
        source_settings={"statusLine": {"type": "command", "command": own_shim}},
        agent_state_dir=state_dir,
        work_dir=work_dir,
        is_local=True,
    )
    assert contribution is not None
    assert "MNGR_USER_STATUSLINE_CMD" not in contribution.env
