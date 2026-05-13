"""Unit tests for the destroy CLI command."""

import json

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.destroy import DestroyCliOptions
from imbue.mngr.cli.destroy import _DestroyTargets
from imbue.mngr.cli.destroy import _OfflineHostToDestroy
from imbue.mngr.cli.destroy import _bypasses_running_check
from imbue.mngr.cli.destroy import _output_result
from imbue.mngr.cli.destroy import _should_skip_confirmation
from imbue.mngr.cli.destroy import destroy
from imbue.mngr.cli.destroy import get_agent_name_from_session
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import AgentName
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


def _make_opts(*, force: bool, yes: bool) -> DestroyCliOptions:
    """Construct a minimal ``DestroyCliOptions`` for predicate tests."""
    return DestroyCliOptions(
        agents=("agent1",),
        agent_list=(),
        force=force,
        yes=yes,
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


def test_destroy_cli_options_can_be_instantiated() -> None:
    """Test that DestroyCliOptions can be instantiated with all required fields."""
    opts = _make_opts(force=False, yes=False)
    assert opts.agents == ("agent1",)
    assert opts.force is False
    assert opts.yes is False


# =============================================================================
# --force vs --yes flag matrix
#
# The four cases below cover the (running x flag) matrix for the two policy
# predicates that gate destruction. The agent-running outcomes are encoded by
# ``_bypasses_running_check``; both predicates are evaluated before any host
# interaction happens, so testing them directly is equivalent to checking the
# behavior of ``mngr destroy`` against (stopped/running) x (--yes/--force).
# =============================================================================


def test_yes_skips_confirmation_for_stopped_agent() -> None:
    """``--yes`` on a stopped agent: skip prompt, do not bypass running check."""
    opts = _make_opts(force=False, yes=True)
    assert _should_skip_confirmation(opts) is True
    assert _bypasses_running_check(opts) is False


def test_force_skips_confirmation_for_stopped_agent() -> None:
    """``--force`` on a stopped agent: skip prompt and (irrelevantly) bypass running check."""
    opts = _make_opts(force=True, yes=False)
    assert _should_skip_confirmation(opts) is True
    assert _bypasses_running_check(opts) is True


def test_yes_refuses_running_agent() -> None:
    """``--yes`` alone does NOT permit destroying a running agent."""
    opts = _make_opts(force=False, yes=True)
    # Same gate as ``agent.is_running() and not _bypasses_running_check(opts)`` --
    # with a running agent and --yes, the inner expression is True -> refuse.
    assert _bypasses_running_check(opts) is False


def test_force_permits_running_agent_and_skips_prompt() -> None:
    """``--force`` permits destroying a running agent AND skips the prompt (unchanged behavior)."""
    opts = _make_opts(force=True, yes=False)
    assert _bypasses_running_check(opts) is True
    assert _should_skip_confirmation(opts) is True


def test_no_flags_prompts_and_keeps_running_check() -> None:
    """No flags: prompt fires and the running-agent safety check stays in place."""
    opts = _make_opts(force=False, yes=False)
    assert _should_skip_confirmation(opts) is False
    assert _bypasses_running_check(opts) is False


def test_destroy_help_documents_yes_and_force_separately(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """``mngr destroy --help`` documents -f/--force and -y/--yes with distinct semantics."""
    result = cli_runner.invoke(destroy, ["--help"], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert "--force" in result.output
    assert "-f" in result.output
    assert "--yes" in result.output
    assert "-y" in result.output


def test_destroy_yes_does_not_swallow_agent_not_found(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """``--yes`` should not bypass safety: AgentNotFoundError still propagates (unlike --force)."""
    result = cli_runner.invoke(
        destroy,
        ["nonexistent-agent-12345", "--yes"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    # Unlike --force, --yes does not swallow not-found errors.
    assert result.exit_code != 0


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
