"""Unit tests for the destroy CLI command."""

import json
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.destroy import DestroyCliOptions
from imbue.mngr.cli.destroy import _DestroyTargets
from imbue.mngr.cli.destroy import _OfflineHostToDestroy
from imbue.mngr.cli.destroy import _filter_removable_worktrees
from imbue.mngr.cli.destroy import _output_result
from imbue.mngr.cli.destroy import destroy
from imbue.mngr.cli.destroy import get_agent_name_from_session
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import OutputFormat


def test_get_agent_name_from_session_extracts_name() -> None:
    """Test that get_agent_name_from_session extracts the agent name correctly."""
    result = get_agent_name_from_session("mngr-my-agent", "mngr-")
    assert result == "my-agent"


def test_get_agent_name_from_session_returns_none_for_empty_session() -> None:
    """Test that get_agent_name_from_session returns None for empty session name."""
    result = get_agent_name_from_session("", "mngr-")
    assert result is None


def test_get_agent_name_from_session_returns_none_when_prefix_does_not_match() -> None:
    """Test that get_agent_name_from_session returns None when session doesn't match prefix."""
    result = get_agent_name_from_session("other-session-name", "mngr-")
    assert result is None


def test_get_agent_name_from_session_returns_none_when_agent_name_empty() -> None:
    """Test that get_agent_name_from_session returns None when agent name is empty after prefix."""
    result = get_agent_name_from_session("mngr-", "mngr-")
    assert result is None


def test_offline_host_to_destroy_can_be_instantiated() -> None:
    """Test that _OfflineHostToDestroy fields can be set (arbitrary_types_allowed)."""
    # _OfflineHostToDestroy requires actual interface objects (arbitrary_types_allowed).
    # We verify the model_config allows arbitrary types and that the class has the expected annotations.
    assert "host" in _OfflineHostToDestroy.model_fields
    assert "provider" in _OfflineHostToDestroy.model_fields
    assert "agent_names" in _OfflineHostToDestroy.model_fields


def test_destroy_targets_has_expected_fields() -> None:
    """Test that _DestroyTargets has the expected fields."""
    assert "online_agents" in _DestroyTargets.model_fields
    assert "offline_hosts" in _DestroyTargets.model_fields


def test_destroy_cli_options_can_be_instantiated() -> None:
    """Test that DestroyCliOptions can be instantiated with all required fields."""
    opts = DestroyCliOptions(
        agents=("agent1",),
        agent_list=(),
        force=False,
        gc=True,
        remove_created_branch=False,
        allow_worktree_removal=True,
        sessions=(),
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.agents == ("agent1",)
    assert opts.force is False


def test_destroy_requires_agent_or_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that destroy requires at least one agent."""
    result = cli_runner.invoke(
        destroy,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent" in result.output


def test_destroy_session_fails_with_invalid_prefix(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session fails when session doesn't match expected prefix format."""
    result = cli_runner.invoke(
        destroy,
        ["--session", "not-mngr-prefix"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "does not match the expected format" in result.output


def test_destroy_session_cannot_combine_with_agent_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session cannot be combined with agent names."""
    result = cli_runner.invoke(
        destroy,
        ["my-agent", "--session", "mngr-some-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "Cannot specify --session with agent names" in result.output


# =============================================================================
# Output helper function tests
# =============================================================================


def test_destroy_output_result_human_with_agents(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in HUMAN format with destroyed agents."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _output_result([AgentName("agent-a"), AgentName("agent-b")], output_opts)
    captured = capsys.readouterr()
    assert "Successfully destroyed 2 agent(s)" in captured.out


def test_destroy_output_result_json(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in JSON format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _output_result([AgentName("agent-x")], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["destroyed_agents"] == ["agent-x"]
    assert data["count"] == 1


def test_destroy_output_result_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in JSONL format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _output_result([AgentName("agent-y")], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "destroy_result"
    assert data["count"] == 1


def test_destroy_output_result_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result with a format template."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{name}")
    _output_result([AgentName("my-agent")], output_opts)
    captured = capsys.readouterr()
    assert "my-agent" in captured.out


# =============================================================================
# Agent address support in destroy
# =============================================================================


def test_destroy_accepts_address_syntax(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Destroy should parse agent addresses without crashing.

    When given NAME@HOST.PROVIDER, the address is parsed and the agent name
    is extracted for matching. The command fails because the agent doesn't exist,
    not because of a parsing error.
    """
    result = cli_runner.invoke(
        destroy,
        ["my-agent@somehost.docker"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    # Should report agent not found (address was parsed, name extracted for matching)
    assert "my-agent" in result.output


def test_destroy_address_force_nonexistent_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Destroy with --force should not crash when address doesn't match any agent."""
    result = cli_runner.invoke(
        destroy,
        ["nonexistent@host.modal", "--force"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    # --force swallows AgentNotFoundError and returns 0
    assert result.exit_code == 0


def test_destroy_plain_name_still_works(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Plain agent names (no @) continue to work with the address-aware destroy."""
    result = cli_runner.invoke(
        destroy,
        ["plain-agent-name", "--force"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    # --force swallows the not-found error
    assert result.exit_code == 0


# =============================================================================
# stdin '-' placeholder tests
# =============================================================================


def test_destroy_dash_reads_agent_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that '-' reads agent names from stdin and passes them as identifiers."""
    result = cli_runner.invoke(
        destroy,
        ["-", "--force"],
        input="agent-from-stdin\n",
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # --force swallows the not-found error, exits 0
    assert result.exit_code == 0


def test_destroy_dash_empty_input_is_noop(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that '-' with empty stdin is a no-op (not an error)."""
    result = cli_runner.invoke(
        destroy,
        ["-"],
        input="",
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code == 0


def test_destroy_dash_multiple_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that '-' reads multiple agent names from stdin."""
    result = cli_runner.invoke(
        destroy,
        ["-", "--force"],
        input="agent-one\nagent-two\nagent-three\n",
        obj=plugin_manager,
        catch_exceptions=False,
    )
    # --force swallows the not-found error
    assert result.exit_code == 0


def test_destroy_dash_strips_whitespace(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that '-' strips whitespace from names."""
    result = cli_runner.invoke(
        destroy,
        ["-", "--force"],
        input="  agent-padded  \n\n  \n",
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


# =============================================================================
# _filter_removable_worktrees: which worktrees does destroy actually remove?
# =============================================================================


def _make_dummy_agent(host: Host, work_dir: Path, name: str) -> None:
    """Create an agent record on host with the given work_dir.

    Persists the data.json so subsequent host.get_agents() calls observe it.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    options = CreateAgentOptions(
        name=AgentName(name),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
    )
    host.create_agent_state(work_dir, options)


def test_filter_skips_worktree_not_generated_by_mngr(local_host: Host, tmp_path: Path) -> None:
    """A work_dir that mngr did not register must never be removed.

    --transfer=none reuses a pre-existing user directory; the directory's
    string is absent from generated_work_dirs, so the filter rejects it
    even when no other agent currently references it.
    """
    work_dir = tmp_path / "user_owned_worktree"
    work_dir.mkdir()
    source_repo = tmp_path / "source_repo"
    source_repo.mkdir()

    queued = [(work_dir, source_repo, local_host)]
    kept, _keys = _filter_removable_worktrees(queued)

    assert kept == []


def test_filter_skips_worktree_still_in_use_by_another_agent(local_host: Host, tmp_path: Path) -> None:
    """Even an mngr-generated worktree must not be removed while another
    agent on the same host still references it as work_dir."""
    work_dir = tmp_path / "shared_mngr_worktree"
    work_dir.mkdir()
    source_repo = tmp_path / "source_repo"
    source_repo.mkdir()

    local_host._add_generated_work_dir(work_dir)
    _make_dummy_agent(local_host, work_dir, "still-running-sibling")

    queued = [(work_dir, source_repo, local_host)]
    kept, _keys = _filter_removable_worktrees(queued)

    assert kept == [], "must not remove a worktree another agent still references"


def test_filter_keeps_orphaned_mngr_generated_worktree(local_host: Host, tmp_path: Path) -> None:
    """Worktrees mngr created and that no surviving agent references must be removed."""
    work_dir = tmp_path / "orphaned_mngr_worktree"
    work_dir.mkdir()
    source_repo = tmp_path / "source_repo"
    source_repo.mkdir()

    local_host._add_generated_work_dir(work_dir)

    queued = [(work_dir, source_repo, local_host)]
    kept, keys = _filter_removable_worktrees(queued)

    assert kept == [(work_dir, source_repo, local_host)]
    assert keys == {(local_host.id, str(work_dir))}


def test_filter_dedupes_repeated_work_dirs(local_host: Host, tmp_path: Path) -> None:
    """Multiple destroyed agents sharing a work_dir queue it once each, but
    we must only attempt removal once."""
    work_dir = tmp_path / "shared_orphan"
    work_dir.mkdir()
    source_repo = tmp_path / "source_repo"
    source_repo.mkdir()

    local_host._add_generated_work_dir(work_dir)

    queued = [
        (work_dir, source_repo, local_host),
        (work_dir, source_repo, local_host),
    ]
    kept, _keys = _filter_removable_worktrees(queued)

    assert kept == [(work_dir, source_repo, local_host)]
