"""Unit tests for mngr_claude_usage.plugin (provisioning helpers + hookimpl filter)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr_claude_usage.plugin import _capture_existing_statusline_command
from imbue.mngr_claude_usage.plugin import _install_settings_local_statusline
from imbue.mngr_claude_usage.plugin import _provision_statusline_shim
from imbue.mngr_claude_usage.plugin import on_before_provisioning


class _StubAgent(BaseModel):
    """Stub agent for the hookimpl filter test (not a real ClaudeAgent)."""

    id: str
    agent_type: str
    work_dir: Path


class _StubHost(BaseModel):
    """Stub host for tests."""

    host_dir: Path


# =============================================================================
# _capture_existing_statusline_command
# =============================================================================


def test_capture_picks_up_command_from_settings_json(local_host: Host, tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": "/path/to/user-statusline.sh"}})
    )
    assert _capture_existing_statusline_command(local_host, tmp_path) == "/path/to/user-statusline.sh"


def test_capture_prefers_settings_local_over_settings_json(local_host: Host, tmp_path: Path) -> None:
    """Local tier wins over project tier in Claude Code's precedence stack."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(json.dumps({"statusLine": {"command": "/project.sh"}}))
    (claude_dir / "settings.local.json").write_text(json.dumps({"statusLine": {"command": "/local.sh"}}))
    assert _capture_existing_statusline_command(local_host, tmp_path) == "/local.sh"


def test_capture_skips_self_recursion(local_host: Host, tmp_path: Path) -> None:
    """On re-provisioning, our own shim is in settings.local.json -- skip it and
    fall through to settings.json so we don't form a recursive wrap."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    own_shim = str(local_host.host_dir / "agents" / "agent-abc" / "commands" / "claude_statusline.sh")
    (claude_dir / "settings.local.json").write_text(json.dumps({"statusLine": {"command": own_shim}}))
    (claude_dir / "settings.json").write_text(json.dumps({"statusLine": {"command": "/user-statusline.sh"}}))
    assert _capture_existing_statusline_command(local_host, tmp_path) == "/user-statusline.sh"


def test_capture_skips_any_prior_agents_shim(local_host: Host, tmp_path: Path) -> None:
    """A *different* prior agent's shim path must also be skipped.

    Regression test for the recursion that bit ``mngr uncapped-claude``: it runs
    in-place in the user's cwd, so ``settings.local.json`` survives across runs
    and on each new invocation has the previous agent's shim path. Without this
    skip, the new agent would capture the previous shim as the "user statusline"
    and chain to it -- which then re-reads
    ``$MNGR_AGENT_STATE_DIR/commands/user_statusline_cmd`` (still the new
    agent's sidecar, because the env var is inherited) and infinite-loops.
    """
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    prior_shim = str(local_host.host_dir / "agents" / "agent-prev1" / "commands" / "claude_statusline.sh")
    (claude_dir / "settings.local.json").write_text(json.dumps({"statusLine": {"command": prior_shim}}))
    assert _capture_existing_statusline_command(local_host, tmp_path) == ""


def test_capture_returns_empty_when_no_settings(local_host: Host, tmp_path: Path) -> None:
    assert _capture_existing_statusline_command(local_host, tmp_path) == ""


def test_capture_tolerates_malformed_json(local_host: Host, tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{not valid json")
    assert _capture_existing_statusline_command(local_host, tmp_path) == ""


# =============================================================================
# _install_settings_local_statusline
# =============================================================================


def test_install_creates_settings_local_when_absent(local_host: Host, tmp_path: Path) -> None:
    _install_settings_local_statusline(local_host, tmp_path, "/path/to/shim.sh")
    settings = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert settings == {"statusLine": {"type": "command", "command": "/path/to/shim.sh"}}


def test_install_merges_into_existing_settings_local(local_host: Host, tmp_path: Path) -> None:
    """Existing keys (hooks, MCP servers, etc.) must be preserved."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text(json.dumps({"hooks": {"SessionStart": "..."}}))
    _install_settings_local_statusline(local_host, tmp_path, "/shim.sh")
    settings = json.loads((claude_dir / "settings.local.json").read_text())
    assert settings["hooks"] == {"SessionStart": "..."}
    assert settings["statusLine"]["command"] == "/shim.sh"


def test_install_overwrites_previous_statusline(local_host: Host, tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.local.json").write_text(json.dumps({"statusLine": {"command": "/old.sh"}}))
    _install_settings_local_statusline(local_host, tmp_path, "/new.sh")
    settings = json.loads((claude_dir / "settings.local.json").read_text())
    assert settings["statusLine"]["command"] == "/new.sh"


# =============================================================================
# _provision_statusline_shim (end-to-end via local_host)
# =============================================================================


def test_provision_creates_shim_writer_and_settings_local(local_host: Host, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _provision_statusline_shim(local_host, state_dir, work_dir)
    commands = state_dir / "commands"
    assert (commands / "claude_statusline.sh").is_file()
    assert (commands / "claude_usage_writer.sh").is_file()
    assert (commands / "claude_statusline.sh").stat().st_mode & 0o111
    assert (commands / "claude_usage_writer.sh").stat().st_mode & 0o111
    settings = json.loads((work_dir / ".claude" / "settings.local.json").read_text())
    assert settings["statusLine"]["command"] == str(commands / "claude_statusline.sh")
    sidecar = commands / "user_statusline_cmd"
    assert sidecar.is_file()
    assert sidecar.read_text() == ""


def test_provision_captures_existing_user_statusline(local_host: Host, tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    work_dir = tmp_path / "work"
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text(json.dumps({"statusLine": {"command": "/path/to/user-statusline.sh"}}))
    _provision_statusline_shim(local_host, state_dir, work_dir)
    sidecar = state_dir / "commands" / "user_statusline_cmd"
    assert sidecar.read_text() == "/path/to/user-statusline.sh"
    project_settings = json.loads((claude_dir / "settings.json").read_text())
    assert project_settings["statusLine"]["command"] == "/path/to/user-statusline.sh"


def test_provision_is_idempotent_on_reprovision(local_host: Host, tmp_path: Path) -> None:
    """Re-running must not capture our own shim as the user command, and must
    preserve the originally captured user command across runs.

    Uses a ``state_dir`` under ``host.host_dir / agents`` to match the real
    layout: production callers always derive ``state_dir`` from
    ``get_agent_state_dir_path(host.host_dir, agent.id)``, and the mngr-owned-
    shim detection in :func:`_capture_existing_statusline_command` keys on
    exactly that prefix.
    """
    state_dir = local_host.host_dir / "agents" / "agent-test1"
    work_dir = tmp_path / "work"
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text(json.dumps({"statusLine": {"command": "/user-statusline.sh"}}))
    _provision_statusline_shim(local_host, state_dir, work_dir)
    _provision_statusline_shim(local_host, state_dir, work_dir)
    assert (state_dir / "commands" / "user_statusline_cmd").read_text() == "/user-statusline.sh"


def test_provision_does_not_chain_prior_agents_shim(local_host: Host, tmp_path: Path) -> None:
    """Re-provisioning in the same ``work_dir`` but with a *different* state_dir
    (a fresh agent, as ``mngr uncapped-claude`` does on every invocation) must
    not pull the previous agent's shim path into the new sidecar -- otherwise
    the chained ``sh -c`` call infinite-loops.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    prior_state_dir = local_host.host_dir / "agents" / "agent-prev"
    _provision_statusline_shim(local_host, prior_state_dir, work_dir)

    new_state_dir = local_host.host_dir / "agents" / "agent-new"
    _provision_statusline_shim(local_host, new_state_dir, work_dir)

    sidecar = new_state_dir / "commands" / "user_statusline_cmd"
    assert sidecar.read_text() == ""


def test_provision_preserves_user_cmd_when_only_in_settings_local(local_host: Host, tmp_path: Path) -> None:
    """User's original statusline lives only in settings.local.json (gitignored
    local tier). First provision captures it, then overwrites settings.local.json
    with our shim. On re-provisioning, the sidecar must still hold the original
    command -- settings.local.json no longer contains it, and falling through to
    an empty settings.json must not silently drop the captured command."""
    state_dir = local_host.host_dir / "agents" / "agent-test2"
    work_dir = tmp_path / "work"
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.local.json").write_text(json.dumps({"statusLine": {"command": "/user-statusline.sh"}}))
    _provision_statusline_shim(local_host, state_dir, work_dir)
    _provision_statusline_shim(local_host, state_dir, work_dir)
    assert (state_dir / "commands" / "user_statusline_cmd").read_text() == "/user-statusline.sh"


def test_hookimpl_skips_non_claude_stub(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """The hookimpl filters with isinstance(agent, ClaudeAgent). Stub agents don't
    pass that check, so the hookimpl is a no-op for them -- no commands dir, no
    settings.local.json. Real ClaudeAgent integration is exercised in mngr_claude's
    own provisioning tests; this test just locks in the filter behavior."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    agent = _StubAgent(id="agent-test", agent_type="opencode", work_dir=work_dir)
    host = _StubHost(host_dir=tmp_path / "host")
    on_before_provisioning(agent=agent, host=host, mngr_ctx=temp_mngr_ctx)  # ty: ignore[invalid-argument-type]
    assert not (tmp_path / "host" / "agents" / "agent-test").exists()
    assert not (work_dir / ".claude" / "settings.local.json").exists()
