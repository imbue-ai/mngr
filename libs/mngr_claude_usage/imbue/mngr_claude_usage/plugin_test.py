"""Unit tests for mngr_claude_usage.plugin (provisioning helpers + hookimpl filter)."""

from __future__ import annotations

import json
from pathlib import Path

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.cli.testing import create_test_agent
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_claude_usage.plugin import _capture_existing_statusline_command
from imbue.mngr_claude_usage.plugin import _install_settings_local_statusline
from imbue.mngr_claude_usage.plugin import _provision_statusline_shim
from imbue.mngr_claude_usage.plugin import _stable_shim_path
from imbue.mngr_claude_usage.plugin import on_before_provisioning

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


def test_capture_skips_stable_shim_path(local_host: Host, tmp_path: Path) -> None:
    """On re-provisioning, our own stable shim path is in settings.local.json --
    skip it and fall through to settings.json so we don't form a recursive wrap."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    own_shim = str(_stable_shim_path(local_host.host_dir))
    (claude_dir / "settings.local.json").write_text(json.dumps({"statusLine": {"command": own_shim}}))
    (claude_dir / "settings.json").write_text(json.dumps({"statusLine": {"command": "/user-statusline.sh"}}))
    assert _capture_existing_statusline_command(local_host, tmp_path) == "/user-statusline.sh"


def test_capture_skips_legacy_per_agent_shim_path(local_host: Host, tmp_path: Path) -> None:
    """Migration: a prior version of this plugin installed the shim under
    ``<host_dir>/agents/<id>/commands/claude_statusline.sh``. A work_dir whose
    settings.local.json still points at that legacy path must not get chained
    into the new sidecar -- otherwise we'd reintroduce the infinite-recursion
    bug the move to a stable path was designed to eliminate."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    legacy_shim = str(local_host.host_dir / "agents" / "agent-prev1" / "commands" / "claude_statusline.sh")
    (claude_dir / "settings.local.json").write_text(json.dumps({"statusLine": {"command": legacy_shim}}))
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


def test_provision_installs_shim_at_host_stable_path(local_host: Host, tmp_path: Path) -> None:
    """The shim and writer scripts land under ``<host_dir>/commands/``, not under
    the per-agent state dir. settings.local.json's statusLine.command points at
    the host-stable path."""
    state_dir = local_host.host_dir / "agents" / "agent-stable-path"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _provision_statusline_shim(local_host, state_dir, work_dir)

    host_commands = local_host.host_dir / "commands"
    assert (host_commands / "claude_statusline.sh").is_file()
    assert (host_commands / "claude_usage_writer.sh").is_file()
    assert (host_commands / "claude_statusline.sh").stat().st_mode & 0o111
    assert (host_commands / "claude_usage_writer.sh").stat().st_mode & 0o111

    settings = json.loads((work_dir / ".claude" / "settings.local.json").read_text())
    assert settings["statusLine"]["command"] == str(host_commands / "claude_statusline.sh")


def test_provision_creates_empty_sidecar_in_state_dir_when_no_user_command(local_host: Host, tmp_path: Path) -> None:
    """The runtime sidecar (``user_statusline_cmd``) lives under the per-agent
    state_dir so the shim can dereference it via ``$MNGR_AGENT_STATE_DIR``."""
    state_dir = local_host.host_dir / "agents" / "agent-empty-sidecar"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    _provision_statusline_shim(local_host, state_dir, work_dir)
    sidecar = state_dir / "commands" / "user_statusline_cmd"
    assert sidecar.is_file()
    assert sidecar.read_text() == ""


def test_provision_captures_existing_user_statusline(local_host: Host, tmp_path: Path) -> None:
    state_dir = local_host.host_dir / "agents" / "agent-capture-user"
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
    (a fresh agent, as ``mngr robinhood`` does on every invocation) must
    not pull the host-stable shim path into the new sidecar -- otherwise the
    shim would invoke itself when chaining and infinite-loop. The provisioner's
    own write_path of settings.local.json IS the stable shim, so capture from
    settings.local.json must skip it on the second run.
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
    with our shim. On re-provisioning the same state_dir, the sidecar must still
    hold the original command -- settings.local.json no longer contains it, and
    falling through to an empty settings.json must not silently drop the
    captured command."""
    state_dir = local_host.host_dir / "agents" / "agent-test2"
    work_dir = tmp_path / "work"
    claude_dir = work_dir / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.local.json").write_text(json.dumps({"statusLine": {"command": "/user-statusline.sh"}}))
    _provision_statusline_shim(local_host, state_dir, work_dir)
    _provision_statusline_shim(local_host, state_dir, work_dir)
    assert (state_dir / "commands" / "user_statusline_cmd").read_text() == "/user-statusline.sh"


# =============================================================================
# on_before_provisioning agent-type filter
# =============================================================================


def test_hookimpl_skips_non_claude_agent(
    local_provider: LocalProviderInstance, temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """The hookimpl filters with isinstance(agent, ClaudeCoreAgent). A real non-Claude
    agent (a plain BaseAgent of the 'generic' type) must be a no-op -- no host
    commands dir, no settings.local.json. The agent runs on a real host, so
    removing the isinstance guard would let provisioning actually run and create
    those artifacts, which these assertions would then catch. Real ClaudeAgent
    provisioning is exercised in mngr_claude's own tests; this test just locks in
    the filter behavior."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    agent = create_test_agent(
        local_provider,
        work_dir,
        agent_config=None,
        agent_type=None,
        extra_data=None,
        agent_class=BaseAgent,
    )

    on_before_provisioning(agent=agent, host=agent.host, mngr_ctx=temp_mngr_ctx)

    assert not (agent.host.host_dir / "commands").exists()
    assert not (work_dir / ".claude" / "settings.local.json").exists()
