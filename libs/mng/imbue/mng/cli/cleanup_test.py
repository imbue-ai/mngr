"""Unit tests for cleanup CLI helpers."""

import json
from collections.abc import Callable

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.api.data_types import CleanupResult
from imbue.mng.cli.cleanup import CleanupCliOptions
from imbue.mng.cli.cleanup import _build_cel_filters_from_options
from imbue.mng.cli.cleanup import _build_cleanup_status_text
from imbue.mng.cli.cleanup import _create_cleanup_list_item
from imbue.mng.cli.cleanup import _emit_dry_run_output
from imbue.mng.cli.cleanup import _emit_no_agents_found
from imbue.mng.cli.cleanup import _emit_result
from imbue.mng.cli.cleanup import _selected_marker
from imbue.mng.cli.cleanup import cleanup
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.interfaces.data_types import AgentDetails
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import CleanupAction
from imbue.mng.primitives import OutputFormat
from imbue.mng.utils.testing import make_test_agent_details

# =============================================================================
# Tests for _build_cel_filters_from_options
# =============================================================================


def _make_opts(
    force: bool = False,
    dry_run: bool = False,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    older_than: str | None = None,
    idle_for: str | None = None,
    tag: tuple[str, ...] = (),
    provider: tuple[str, ...] = (),
    agent_type: tuple[str, ...] = (),
    action: str = "destroy",
    snapshot_before: bool = False,
) -> CleanupCliOptions:
    """Create a CleanupCliOptions with defaults and specified overrides."""
    return CleanupCliOptions(
        force=force,
        dry_run=dry_run,
        include=include,
        exclude=exclude,
        older_than=older_than,
        idle_for=idle_for,
        tag=tag,
        provider=provider,
        agent_type=agent_type,
        action=action,
        snapshot_before=snapshot_before,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )


def test_build_cel_filters_no_options() -> None:
    opts = _make_opts()
    include_filters, exclude_filters = _build_cel_filters_from_options(opts)
    assert include_filters == []
    assert exclude_filters == []


def test_build_cel_filters_older_than() -> None:
    opts = _make_opts(older_than="7d")
    include_filters, exclude_filters = _build_cel_filters_from_options(opts)
    assert "age > 604800.0" in include_filters
    assert exclude_filters == []


def test_build_cel_filters_idle_for() -> None:
    opts = _make_opts(idle_for="1h")
    include_filters, exclude_filters = _build_cel_filters_from_options(opts)
    assert "idle > 3600.0" in include_filters


def test_build_cel_filters_single_provider() -> None:
    opts = _make_opts(provider=("docker",))
    include_filters, exclude_filters = _build_cel_filters_from_options(opts)
    assert 'host.provider == "docker"' in include_filters


def test_build_cel_filters_multiple_providers() -> None:
    opts = _make_opts(provider=("docker", "modal"))
    include_filters, exclude_filters = _build_cel_filters_from_options(opts)
    assert any("docker" in f and "modal" in f for f in include_filters)


def test_build_cel_filters_single_agent_type() -> None:
    opts = _make_opts(agent_type=("claude",))
    include_filters, exclude_filters = _build_cel_filters_from_options(opts)
    assert 'type == "claude"' in include_filters


def test_build_cel_filters_multiple_agent_types() -> None:
    opts = _make_opts(agent_type=("claude", "codex"))
    include_filters, exclude_filters = _build_cel_filters_from_options(opts)
    assert any("claude" in f and "codex" in f for f in include_filters)


def test_build_cel_filters_tag_with_value() -> None:
    opts = _make_opts(tag=("env=prod",))
    include_filters, exclude_filters = _build_cel_filters_from_options(opts)
    assert 'host.tags.env == "prod"' in include_filters


def test_build_cel_filters_tag_without_value() -> None:
    opts = _make_opts(tag=("ephemeral",))
    include_filters, exclude_filters = _build_cel_filters_from_options(opts)
    assert 'host.tags.ephemeral == "true"' in include_filters


def test_build_cel_filters_include_and_exclude_passthrough() -> None:
    opts = _make_opts(include=('state == "RUNNING"',), exclude=('name == "keep"',))
    include_filters, exclude_filters = _build_cel_filters_from_options(opts)
    assert 'state == "RUNNING"' in include_filters
    assert 'name == "keep"' in exclude_filters


def test_build_cel_filters_combined() -> None:
    opts = _make_opts(older_than="7d", provider=("docker",), agent_type=("claude",))
    include_filters, exclude_filters = _build_cel_filters_from_options(opts)
    assert len(include_filters) == 3
    assert "age > 604800.0" in include_filters
    assert 'host.provider == "docker"' in include_filters
    assert 'type == "claude"' in include_filters


# =============================================================================
# Tests for _selected_marker
# =============================================================================


def test_selected_marker_true() -> None:
    assert _selected_marker(True) == "[x]"


def test_selected_marker_false() -> None:
    assert _selected_marker(False) == "[ ]"


# =============================================================================
# Tests for _create_cleanup_list_item
# =============================================================================


def test_create_cleanup_list_item_selected_contains_marker_and_agent_name() -> None:
    """A selected item should contain [x] and the agent name in its display text."""
    agent = make_test_agent_details(name="test-agent", state=AgentLifecycleState.RUNNING)
    item = _create_cleanup_list_item(agent, is_selected=True, name_width=20, state_width=10, provider_width=10)
    display_text = item.original_widget.text
    assert "[x]" in display_text
    assert "test-agent" in display_text


def test_create_cleanup_list_item_not_selected_contains_empty_marker() -> None:
    """An unselected item should contain [ ] and the agent name in its display text."""
    agent = make_test_agent_details(name="other-agent", state=AgentLifecycleState.STOPPED)
    item = _create_cleanup_list_item(agent, is_selected=False, name_width=20, state_width=10, provider_width=10)
    display_text = item.original_widget.text
    assert "[ ]" in display_text
    assert "other-agent" in display_text
    assert "STOPPED" in display_text


# =============================================================================
# Tests for CLI options model
# =============================================================================


def test_cleanup_cli_options_fields() -> None:
    opts = _make_opts()
    assert opts.force is False
    assert opts.dry_run is False
    assert opts.action == "destroy"
    assert opts.older_than is None
    assert opts.idle_for is None
    assert opts.tag == ()
    assert opts.provider == ()
    assert opts.agent_type == ()
    assert opts.snapshot_before is False


# =============================================================================
# Tests for CLI command invocation
# =============================================================================


def test_cleanup_dry_run_yes_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --dry-run --yes with no agents reports none found."""
    result = cli_runner.invoke(
        cleanup,
        ["--dry-run", "--yes"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "no agents found" in result.output.lower()


# =============================================================================
# Dry-run output formatting tests (monkeypatched agents, no real providers)
# =============================================================================


@pytest.fixture
def patch_find_agents(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[AgentDetails]], None]:
    """Return a callable that patches find_agents_for_cleanup to return the given agents."""

    def _patch(agents: list[AgentDetails]) -> None:
        monkeypatch.setattr(
            "imbue.mng.cli.cleanup.find_agents_for_cleanup",
            lambda **kwargs: agents,
        )

    return _patch


def test_cleanup_dry_run_human_format_with_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patch_find_agents: Callable[[list[AgentDetails]], None],
) -> None:
    """--dry-run --yes should list agents that would be destroyed in human format."""
    agents = [
        make_test_agent_details(name="cleanup-alpha", state=AgentLifecycleState.RUNNING),
        make_test_agent_details(name="cleanup-beta", state=AgentLifecycleState.STOPPED),
    ]
    patch_find_agents(agents)

    result = cli_runner.invoke(
        cleanup,
        ["--dry-run", "--yes"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Would destroy" in result.output
    assert "cleanup-alpha" in result.output
    assert "cleanup-beta" in result.output


def test_cleanup_dry_run_stop_action_human_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patch_find_agents: Callable[[list[AgentDetails]], None],
) -> None:
    """--stop --dry-run --yes should say 'Would stop' in human format."""
    agents = [
        make_test_agent_details(name="stop-me", state=AgentLifecycleState.RUNNING),
    ]
    patch_find_agents(agents)

    result = cli_runner.invoke(
        cleanup,
        ["--stop", "--dry-run", "--yes"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "Would stop" in result.output
    assert "stop-me" in result.output


def test_cleanup_dry_run_json_format_with_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patch_find_agents: Callable[[list[AgentDetails]], None],
) -> None:
    """--dry-run --yes --format json should emit structured JSON."""
    agents = [
        make_test_agent_details(name="json-agent", state=AgentLifecycleState.RUNNING),
    ]
    patch_find_agents(agents)

    result = cli_runner.invoke(
        cleanup,
        ["--dry-run", "--yes", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    output = json.loads(result.output.strip())
    assert output["dry_run"] is True
    assert output["action"] == "destroy"
    assert len(output["agents"]) == 1
    assert output["agents"][0]["name"] == "json-agent"


def test_cleanup_dry_run_jsonl_format_with_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patch_find_agents: Callable[[list[AgentDetails]], None],
) -> None:
    """--dry-run --yes --format jsonl should emit JSONL events."""
    agents = [
        make_test_agent_details(name="jsonl-agent", state=AgentLifecycleState.RUNNING),
    ]
    patch_find_agents(agents)

    result = cli_runner.invoke(
        cleanup,
        ["--dry-run", "--yes", "--format", "jsonl"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    # Should have at least the dry_run event (may also have info events)
    dry_run_events = [json.loads(line) for line in lines if "dry_run" in line]
    assert len(dry_run_events) == 1
    assert dry_run_events[0]["event"] == "dry_run"
    assert dry_run_events[0]["action"] == "destroy"
    assert len(dry_run_events[0]["agents"]) == 1


def test_cleanup_dry_run_stop_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patch_find_agents: Callable[[list[AgentDetails]], None],
) -> None:
    """--stop --dry-run --yes --format json should report action as 'stop'."""
    agents = [
        make_test_agent_details(name="stop-json", state=AgentLifecycleState.RUNNING),
    ]
    patch_find_agents(agents)

    result = cli_runner.invoke(
        cleanup,
        ["--stop", "--dry-run", "--yes", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    output = json.loads(result.output.strip())
    assert output["action"] == "stop"
    assert output["dry_run"] is True


# =============================================================================
# --yes --force and no-agents output format tests
# =============================================================================


def test_cleanup_yes_force_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patch_find_agents: Callable[[list[AgentDetails]], None],
) -> None:
    """--yes --force with no agents returns 0 and reports no agents found."""
    patch_find_agents([])

    result = cli_runner.invoke(
        cleanup,
        ["--yes", "--force"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "no agents found" in result.output.lower()


def test_cleanup_json_output_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patch_find_agents: Callable[[list[AgentDetails]], None],
) -> None:
    """--format json with no agents emits JSON with empty agents list."""
    patch_find_agents([])

    result = cli_runner.invoke(
        cleanup,
        ["--yes", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    output = json.loads(result.output.strip())
    assert output["agents"] == []
    assert output["message"] == "No agents found"


def test_cleanup_jsonl_output_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patch_find_agents: Callable[[list[AgentDetails]], None],
) -> None:
    """--format jsonl with no agents emits a JSONL info event."""
    patch_find_agents([])

    result = cli_runner.invoke(
        cleanup,
        ["--yes", "--format", "jsonl"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    lines = [line for line in result.output.strip().split("\n") if line.strip()]
    info_events = [json.loads(line) for line in lines if "info" in line]
    assert len(info_events) >= 1
    assert any(e.get("message") == "No agents found" for e in info_events)


# =============================================================================
# Tests for _build_cleanup_status_text
# =============================================================================


def test_build_cleanup_status_text_destroy_action() -> None:
    """Test status text for destroy action."""
    text = _build_cleanup_status_text(
        search_query="",
        hide_stopped=False,
        selected_count=3,
        total_count=10,
        action=CleanupAction.DESTROY,
    )
    assert "3/10" in text
    assert "destroy" in text


def test_build_cleanup_status_text_stop_action() -> None:
    """Test status text for stop action."""
    text = _build_cleanup_status_text(
        search_query="my-query",
        hide_stopped=True,
        selected_count=1,
        total_count=5,
        action=CleanupAction.STOP,
    )
    assert "1/5" in text
    assert "stop" in text
    assert "my-query" in text


# =============================================================================
# Tests for _emit_result (direct function tests)
# =============================================================================


def test_emit_result_human_destroyed(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_result with destroyed agents in HUMAN format."""
    result = CleanupResult(
        destroyed_agents=[AgentName("agent-a"), AgentName("agent-b")],
        stopped_agents=[],
        errors=[],
    )
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_result(result, output_opts)
    captured = capsys.readouterr()
    output = captured.out
    assert "Successfully destroyed 2 agent(s)" in output
    assert "agent-a" in output
    assert "agent-b" in output


def test_emit_result_human_stopped(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_result with stopped agents in HUMAN format."""
    result = CleanupResult(
        destroyed_agents=[],
        stopped_agents=[AgentName("stopped-agent")],
        errors=[],
    )
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_result(result, output_opts)
    captured = capsys.readouterr()
    output = captured.out
    assert "Successfully stopped 1 agent(s)" in output
    assert "stopped-agent" in output


def test_emit_result_human_no_agents(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_result with no agents affected in HUMAN format."""
    result = CleanupResult(
        destroyed_agents=[],
        stopped_agents=[],
        errors=[],
    )
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_result(result, output_opts)
    captured = capsys.readouterr()
    output = captured.out
    assert "No agents were affected" in output


def test_emit_result_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_result in JSON format."""
    result = CleanupResult(
        destroyed_agents=[AgentName("agent-x")],
        stopped_agents=[],
        errors=["some error"],
    )
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_result(result, output_opts)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["destroyed_agents"] == ["agent-x"]
    assert output["destroyed_count"] == 1
    assert output["error_count"] == 1


def test_emit_result_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_result in JSONL format."""
    result = CleanupResult(
        destroyed_agents=[],
        stopped_agents=[AgentName("agent-y")],
        errors=[],
    )
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _emit_result(result, output_opts)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "cleanup_result"
    assert output["stopped_agents"] == ["agent-y"]
    assert output["stopped_count"] == 1


# =============================================================================
# Tests for _emit_dry_run_output (direct function tests)
# =============================================================================


def test_emit_dry_run_output_human_destroy(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_dry_run_output with destroy action in HUMAN format."""
    agents = [
        make_test_agent_details(name="dry-run-agent", state=AgentLifecycleState.RUNNING),
    ]
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_dry_run_output(agents, CleanupAction.DESTROY, output_opts)
    captured = capsys.readouterr()
    output = captured.out
    assert "Would destroy" in output
    assert "dry-run-agent" in output


def test_emit_dry_run_output_human_stop(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_dry_run_output with stop action in HUMAN format."""
    agents = [
        make_test_agent_details(name="stop-target", state=AgentLifecycleState.RUNNING),
    ]
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_dry_run_output(agents, CleanupAction.STOP, output_opts)
    captured = capsys.readouterr()
    output = captured.out
    assert "Would stop" in output
    assert "stop-target" in output


def test_emit_dry_run_output_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_dry_run_output in JSON format."""
    agents = [
        make_test_agent_details(name="json-dry", state=AgentLifecycleState.RUNNING),
    ]
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_dry_run_output(agents, CleanupAction.DESTROY, output_opts)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["dry_run"] is True
    assert output["action"] == "destroy"
    assert len(output["agents"]) == 1


def test_emit_dry_run_output_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _emit_dry_run_output in JSONL format."""
    agents = [
        make_test_agent_details(name="jsonl-dry", state=AgentLifecycleState.RUNNING),
    ]
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _emit_dry_run_output(agents, CleanupAction.STOP, output_opts)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "dry_run"
    assert output["action"] == "stop"


# =============================================================================
# Tests for _emit_no_agents_found
# =============================================================================


def test_emit_no_agents_found_human_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_no_agents_found should output message in HUMAN format."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_no_agents_found(output_opts)
    captured = capsys.readouterr()
    assert "No agents found matching the specified filters" in captured.out


def test_emit_no_agents_found_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_no_agents_found should output JSON data."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_no_agents_found(output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["agents"] == []
    assert "No agents found" in data["message"]


def test_emit_no_agents_found_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_no_agents_found should output JSONL event."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _emit_no_agents_found(output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "info"
    assert "No agents found" in data["message"]


# =============================================================================
# Tests for _emit_result (additional scenarios)
# =============================================================================


def test_emit_result_json_with_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_result should include errors in JSON data."""
    result = CleanupResult()
    result.destroyed_agents = [AgentName("agent-x")]
    result.stopped_agents = [AgentName("agent-y")]
    result.errors = ["error-1"]
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_result(result, output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["destroyed_count"] == 1
    assert data["stopped_count"] == 1
    assert data["error_count"] == 1
    assert data["errors"] == ["error-1"]


def test_emit_result_human_with_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_result should display errors in HUMAN format."""
    result = CleanupResult()
    result.errors = ["Failed to destroy agent-x", "Timeout on agent-y"]
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_result(result, output_opts)
    captured = capsys.readouterr()
    assert "No agents were affected" in captured.out
