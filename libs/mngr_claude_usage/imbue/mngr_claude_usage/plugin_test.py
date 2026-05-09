"""Unit tests for mngr_claude_usage.plugin (hookimpl + provisioning helpers)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_claude_usage.plugin import _capture_existing_statusline_command
from imbue.mngr_claude_usage.plugin import _install_settings_local_statusline
from imbue.mngr_claude_usage.plugin import on_before_provisioning


class _StubAgent(BaseModel):
    """Stub matching the duck-typed interface the hookimpl needs from AgentInterface."""

    id: str
    agent_type: str
    work_dir: Path


class _StubHost(BaseModel):
    """Stub matching the duck-typed interface the hookimpl needs from OnlineHostInterface."""

    host_dir: Path
    is_local: bool


# =============================================================================
# _capture_existing_statusline_command
# =============================================================================


def test_capture_picks_up_command_from_settings_json(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "/path/to/caveman.sh"}})
    )
    assert _capture_existing_statusline_command(tmp_path, our_shim_path="/different/shim.sh") == "/path/to/caveman.sh"


def test_capture_prefers_settings_local_over_settings_json(tmp_path: Path) -> None:
    """Local tier wins over project tier in Claude Code's precedence stack."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"statusLine": {"command": "/project.sh"}}))
    (claude_dir / "settings.local.json").write_text(json.dumps({"statusLine": {"command": "/local.sh"}}))
    assert _capture_existing_statusline_command(tmp_path, our_shim_path="/different.sh") == "/local.sh"


def test_capture_skips_self_recursion(tmp_path: Path) -> None:
    """On re-provisioning, our own shim is in settings.local.json -- skip it and
    fall through to settings.json so we don't form a recursive wrap."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    own_shim = "/state/commands/claude_statusline.sh"
    (claude_dir / "settings.local.json").write_text(json.dumps({"statusLine": {"command": own_shim}}))
    (claude_dir / "settings.json").write_text(json.dumps({"statusLine": {"command": "/caveman.sh"}}))
    assert _capture_existing_statusline_command(tmp_path, our_shim_path=own_shim) == "/caveman.sh"


def test_capture_returns_empty_when_no_settings(tmp_path: Path) -> None:
    assert _capture_existing_statusline_command(tmp_path, our_shim_path="/x") == ""


def test_capture_tolerates_malformed_json(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{not valid json")
    assert _capture_existing_statusline_command(tmp_path, our_shim_path="/x") == ""


# =============================================================================
# _install_settings_local_statusline
# =============================================================================


def test_install_creates_settings_local_when_absent(tmp_path: Path) -> None:
    _install_settings_local_statusline(tmp_path, "/path/to/shim.sh")
    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert settings == {"statusLine": {"type": "command", "command": "/path/to/shim.sh"}}


def test_install_merges_into_existing_settings_local(tmp_path: Path) -> None:
    """Existing keys (hooks, MCP servers, etc.) must be preserved."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text(json.dumps({"hooks": {"SessionStart": "..."}}))
    _install_settings_local_statusline(tmp_path, "/shim.sh")
    settings = json.loads((claude_dir / "settings.local.json").read_text())
    assert settings["hooks"] == {"SessionStart": "..."}
    assert settings["statusLine"]["command"] == "/shim.sh"


def test_install_overwrites_previous_statusline(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text(json.dumps({"statusLine": {"command": "/old.sh"}}))
    _install_settings_local_statusline(tmp_path, "/new.sh")
    settings = json.loads((claude_dir / "settings.local.json").read_text())
    assert settings["statusLine"]["command"] == "/new.sh"


# =============================================================================
# on_before_provisioning (end-to-end)
# =============================================================================


def _run_hook(tmp_path: Path, mngr_ctx: MngrContext, *, agent_type: str = "claude", is_local: bool = True) -> Path:
    """Invoke the hookimpl with a stub agent + host. Returns the agent's state dir."""
    host_dir = tmp_path / "host"
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    agent = _StubAgent(id="agent-test", agent_type=agent_type, work_dir=work_dir)
    host = _StubHost(host_dir=host_dir, is_local=is_local)
    on_before_provisioning(agent=agent, host=host, mngr_ctx=mngr_ctx)  # ty: ignore[invalid-argument-type]
    return host_dir / "agents" / "agent-test"


def test_hookimpl_provisions_shim_writer_and_settings_local(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    state_dir = _run_hook(tmp_path, temp_mngr_ctx)
    commands = state_dir / "commands"
    assert (commands / "claude_statusline.sh").is_file()
    assert (commands / "claude_rate_limits_writer.sh").is_file()
    # Both must be executable.
    assert (commands / "claude_statusline.sh").stat().st_mode & 0o111
    assert (commands / "claude_rate_limits_writer.sh").stat().st_mode & 0o111
    # settings.local.json points at the shim.
    settings_local = tmp_path / "work" / ".claude" / "settings.local.json"
    settings = json.loads(settings_local.read_text())
    assert settings["statusLine"]["command"] == str(commands / "claude_statusline.sh")
    # No user statusline was present, so the sidecar is empty.
    sidecar = commands / "user_statusline_cmd"
    assert sidecar.is_file()
    assert sidecar.read_text() == ""


def test_hookimpl_captures_existing_user_statusline(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"statusLine": {"command": "/path/to/caveman.sh"}}))
    state_dir = _run_hook(tmp_path, temp_mngr_ctx)
    sidecar = state_dir / "commands" / "user_statusline_cmd"
    assert sidecar.read_text() == "/path/to/caveman.sh"
    # The wrapping shim is now installed, but settings.json (project tier) is unchanged.
    project_settings = json.loads((claude_dir / "settings.json").read_text())
    assert project_settings["statusLine"]["command"] == "/path/to/caveman.sh"


def test_hookimpl_skips_non_claude_agents(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    state_dir = _run_hook(tmp_path, temp_mngr_ctx, agent_type="opencode")
    assert not (state_dir / "commands").exists()
    assert not (tmp_path / "work" / ".claude" / "settings.local.json").exists()


def test_hookimpl_skips_remote_hosts(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    state_dir = _run_hook(tmp_path, temp_mngr_ctx, is_local=False)
    assert not (state_dir / "commands").exists()
    assert not (tmp_path / "work" / ".claude" / "settings.local.json").exists()


def test_hookimpl_is_idempotent_on_reprovision(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """Re-running provisioning must not capture our own shim as the user command,
    and must preserve the originally captured user command across runs."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"statusLine": {"command": "/caveman.sh"}}))
    state_dir = _run_hook(tmp_path, temp_mngr_ctx)
    state_dir = _run_hook(tmp_path, temp_mngr_ctx)
    assert (state_dir / "commands" / "user_statusline_cmd").read_text() == "/caveman.sh"
