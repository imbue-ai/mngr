"""Unit tests for cleanup CLI helpers."""

import json
from collections.abc import Callable

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.cleanup import CleanupCliOptions
from imbue.mng.cli.cleanup import _build_cel_filters_from_options
from imbue.mng.cli.cleanup import _selected_marker
from imbue.mng.cli.cleanup import cleanup
from imbue.mng.cli.conftest import make_test_agent_info
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.primitives import AgentLifecycleState

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


def test_cleanup_help_exits_zero(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --help works and exits 0."""
    result = cli_runner.invoke(
        cleanup,
        ["--help"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "cleanup" in result.output.lower()


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


def test_cleanup_snapshot_before_raises_not_implemented(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --snapshot-before raises NotImplementedError."""
    result = cli_runner.invoke(
        cleanup,
        ["--snapshot-before", "--yes"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


# =============================================================================
# Dry-run output formatting tests (monkeypatched agents, no real providers)
# =============================================================================


@pytest.fixture
def patch_find_agents(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[AgentInfo]], None]:
    """Return a callable that patches find_agents_for_cleanup to return the given agents."""

    def _patch(agents: list[AgentInfo]) -> None:
        monkeypatch.setattr(
            "imbue.mng.cli.cleanup.find_agents_for_cleanup",
            lambda **kwargs: agents,
        )

    return _patch


def test_cleanup_dry_run_human_format_with_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    patch_find_agents: Callable[[list[AgentInfo]], None],
) -> None:
    """--dry-run --yes should list agents that would be destroyed in human format."""
    agents = [
        make_test_agent_info(name="cleanup-alpha", state=AgentLifecycleState.RUNNING),
        make_test_agent_info(name="cleanup-beta", state=AgentLifecycleState.STOPPED),
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
    patch_find_agents: Callable[[list[AgentInfo]], None],
) -> None:
    """--stop --dry-run --yes should say 'Would stop' in human format."""
    agents = [
        make_test_agent_info(name="stop-me", state=AgentLifecycleState.RUNNING),
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
    patch_find_agents: Callable[[list[AgentInfo]], None],
) -> None:
    """--dry-run --yes --format json should emit structured JSON."""
    agents = [
        make_test_agent_info(name="json-agent", state=AgentLifecycleState.RUNNING),
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
    patch_find_agents: Callable[[list[AgentInfo]], None],
) -> None:
    """--dry-run --yes --format jsonl should emit JSONL events."""
    agents = [
        make_test_agent_info(name="jsonl-agent", state=AgentLifecycleState.RUNNING),
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
    patch_find_agents: Callable[[list[AgentInfo]], None],
) -> None:
    """--stop --dry-run --yes --format json should report action as 'stop'."""
    agents = [
        make_test_agent_info(name="stop-json", state=AgentLifecycleState.RUNNING),
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
