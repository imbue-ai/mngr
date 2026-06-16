"""Unit tests for the stop CLI command."""

import json
from collections.abc import Callable
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.discovery_events import write_full_discovery_snapshot
from imbue.mngr.cli.stop import StopCliOptions
from imbue.mngr.cli.stop import _ensure_providers_support_host_shutdown
from imbue.mngr.cli.stop import _output_result
from imbue.mngr.cli.stop import _stop_hosts_for_addresses
from imbue.mngr.cli.stop import stop
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.errors import AgentNotFoundError
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import HostShutdownNotSupportedError
from imbue.mngr.errors import LocalHostNotStoppableError
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LOCAL_HOST_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.providers.mock_provider_test import MockProviderInstance


def test_stop_cli_options_fields() -> None:
    """Test StopCliOptions has required fields."""
    opts = StopCliOptions(
        agents=("agent1", "agent2"),
        agent_list=(AgentAddress(agent=AgentName("agent3")),),
        archive=False,
        sessions=(),
        stop_host=False,
        dry_run=False,
        snapshot_mode=None,
        graceful=True,
        graceful_timeout=None,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.agents == ("agent1", "agent2")
    assert opts.agent_list == (AgentAddress(agent=AgentName("agent3")),)
    assert opts.sessions == ()


def test_stop_requires_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that stop requires at least one agent."""
    result = cli_runner.invoke(
        stop,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent (use '-' to read from stdin)" in result.output


def test_stop_session_cannot_combine_with_agent_names(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session cannot be combined with agent names."""
    result = cli_runner.invoke(
        stop,
        ["my-agent", "--session", "mngr-some-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot specify --session with agent names" in result.output


def test_stop_session_fails_with_invalid_prefix(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --session fails when session doesn't match expected prefix format."""
    result = cli_runner.invoke(
        stop,
        ["--session", "other-session-name"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "does not match the expected format" in result.output


def test_stop_host_rejects_archive_combination(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--stop-host and --archive cannot be used together."""
    result = cli_runner.invoke(
        stop,
        ["my-agent", "--stop-host", "--archive"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot use --stop-host together with --archive" in result.output


# =============================================================================
# Host-shutdown capability validation
# =============================================================================


def _make_mock_provider(
    name: str,
    supports_shutdown: bool,
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> MockProviderInstance:
    return MockProviderInstance(
        name=ProviderInstanceName(name),
        host_dir=temp_host_dir,
        mngr_ctx=temp_mngr_ctx,
        mock_supports_shutdown_hosts=supports_shutdown,
    )


def test_ensure_providers_support_host_shutdown_passes_when_all_support(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """No error is raised when every provider supports stopping hosts."""
    providers = [
        _make_mock_provider("p1", True, temp_host_dir, temp_mngr_ctx),
        _make_mock_provider("p2", True, temp_host_dir, temp_mngr_ctx),
    ]
    _ensure_providers_support_host_shutdown(providers)


def test_ensure_providers_support_host_shutdown_raises_for_unsupported(
    temp_host_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A provider that cannot stop hosts triggers HostShutdownNotSupportedError."""
    providers = [
        _make_mock_provider("good", True, temp_host_dir, temp_mngr_ctx),
        _make_mock_provider("bad", False, temp_host_dir, temp_mngr_ctx),
    ]
    with pytest.raises(HostShutdownNotSupportedError) as exc_info:
        _ensure_providers_support_host_shutdown(providers)
    assert exc_info.value.provider_name == ProviderInstanceName("bad")


# =============================================================================
# --stop-host SSH-free host resolution
# =============================================================================


def _seed_local_agent_snapshot(
    mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    agent_name: str,
) -> None:
    """Write a DISCOVERY_FULL snapshot with one agent on the real local host.

    This is the only state ``--stop-host`` needs: it resolves the host from
    the event stream, without ever enumerating agents over SSH.
    """
    host_id = local_provider.host_id
    agent = DiscoveredAgent(
        host_id=host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName(agent_name),
        provider_name=ProviderInstanceName("local"),
        certified_data={},
    )
    host = DiscoveredHost(
        host_id=host_id,
        host_name=HostName(LOCAL_HOST_NAME),
        provider_name=ProviderInstanceName("local"),
    )
    write_full_discovery_snapshot(mngr_ctx.config, [agent], [host])


def test_stop_hosts_for_addresses_routes_to_provider_stop_host(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """``--stop-host`` resolves the host from the event stream and calls stop_host.

    The agent is never started -- only a discovery snapshot exists -- which
    proves the host is resolved without any agent enumeration. The local
    provider rejects the actual stop, and reaching that rejection proves the
    call routed all the way through to ``provider.stop_host``.
    """
    _seed_local_agent_snapshot(temp_mngr_ctx, local_provider, "stop-host-agent")

    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    with pytest.raises(LocalHostNotStoppableError):
        _stop_hosts_for_addresses(
            [AgentAddress(agent=AgentName("stop-host-agent"))],
            temp_mngr_ctx,
            output_opts,
        )


def test_stop_hosts_for_addresses_raises_for_unknown_agent(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """An agent identifier absent from the event stream raises AgentNotFoundError."""
    _seed_local_agent_snapshot(temp_mngr_ctx, local_provider, "known-agent")

    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    with pytest.raises(AgentNotFoundError):
        _stop_hosts_for_addresses(
            [AgentAddress(agent=AgentName("missing-agent"))],
            temp_mngr_ctx,
            output_opts,
        )


def test_stop_hosts_for_addresses_raises_when_host_no_longer_exists(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """A stale event stream pointing at a vanished host surfaces as an error.

    Resolution maps the agent to a recorded host_id without checking that the
    host still exists; the provider's SSH-free ``get_host`` is what validates
    it, raising ``HostNotFoundError`` when the host is gone -- so ``--stop-host``
    fails loudly instead of silently stopping nothing.
    """
    stale_host_id = HostId.generate()
    agent = DiscoveredAgent(
        host_id=stale_host_id,
        agent_id=AgentId.generate(),
        agent_name=AgentName("orphan-agent"),
        provider_name=ProviderInstanceName("local"),
        certified_data={},
    )
    host = DiscoveredHost(
        host_id=stale_host_id,
        host_name=HostName(LOCAL_HOST_NAME),
        provider_name=ProviderInstanceName("local"),
    )
    write_full_discovery_snapshot(temp_mngr_ctx.config, [agent], [host])

    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    with pytest.raises(HostNotFoundError):
        _stop_hosts_for_addresses(
            [AgentAddress(agent=AgentName("orphan-agent"))],
            temp_mngr_ctx,
            output_opts,
        )


def test_stop_host_uses_ssh_free_resolution(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
) -> None:
    """``mngr stop --stop-host`` resolves the host from the event stream only.

    With only a discovery snapshot on disk (no running agent, no tmux), the
    command still reaches ``provider.stop_host`` -- proving the ``--stop-host``
    path never performs the agent-enumeration scan that would SSH into the
    host. The local provider then rejects the stop, surfacing as a non-zero
    exit with the local-host error message.
    """
    _seed_local_agent_snapshot(temp_mngr_ctx, local_provider, "ssh-free-agent")

    result = cli_runner.invoke(
        stop,
        ["ssh-free-agent", "--stop-host"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot stop the local host" in result.output


# =============================================================================
# StopCliOptions additional field tests
# =============================================================================


def test_stop_cli_options_accepts_all_optional_fields() -> None:
    """Test StopCliOptions can be instantiated with all optional fields set."""
    opts = StopCliOptions(
        agents=("a1", "a2", "a3"),
        agent_list=(AgentAddress(agent=AgentName("a4")),),
        archive=True,
        sessions=("mngr-session-1", "mngr-session-2"),
        stop_host=True,
        dry_run=False,
        snapshot_mode="auto",
        graceful=False,
        graceful_timeout="30s",
        output_format="json",
        quiet=True,
        verbose=2,
        log_file=None,
        log_commands=None,
        plugin=("my-plugin",),
        disable_plugin=("other-plugin",),
    )
    assert opts.agents == ("a1", "a2", "a3")
    assert opts.sessions == ("mngr-session-1", "mngr-session-2")
    assert opts.snapshot_mode == "auto"
    assert opts.graceful is False
    assert opts.graceful_timeout == "30s"
    assert opts.quiet is True
    assert opts.verbose == 2
    assert opts.plugin == ("my-plugin",)
    assert opts.disable_plugin == ("other-plugin",)


# =============================================================================
# Output helper function tests
# =============================================================================


def test_stop_output_result_human_with_agents(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in HUMAN format with stopped agents."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _output_result(["agent-1", "agent-2"], [], output_opts)
    captured = capsys.readouterr()
    assert "Successfully stopped 2 agent(s)" in captured.out


def test_stop_output_result_human_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in HUMAN format with no agents outputs nothing."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _output_result([], [], output_opts)
    captured = capsys.readouterr()
    # With no agents, the HUMAN output does not write a success message
    assert "Successfully stopped" not in captured.out


def test_stop_output_result_json(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in JSON format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _output_result(["agent-x"], [], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["stopped_agents"] == ["agent-x"]
    assert data["count"] == 1
    assert data["failures"] == []
    assert data["failure_count"] == 0
    assert data["exit_code"] == 0


def test_stop_output_result_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result in JSONL format."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _output_result(["agent-a"], [], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "stop_result"
    assert data["count"] == 1


def test_stop_output_result_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """Test _output_result with a format template."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{name}")
    _output_result(["template-agent"], [], output_opts)
    captured = capsys.readouterr()
    assert "template-agent" in captured.out


def test_stop_output_result_json_reports_failures(capsys: pytest.CaptureFixture[str]) -> None:
    """_output_result emits the structured failures payload (failures / failure_count / exit_code)."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    host_id = HostId.generate()
    failure = CleanupFailure(
        category=CleanupFailureCategory.PROCESSES_REMAIN,
        message="kill -KILL failed: operation not permitted",
        agent_name=AgentName("agent-z"),
        host_id=host_id,
    )
    _output_result(["agent-z"], [failure], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["failure_count"] == 1
    assert data["failures"] == [
        {
            "category": "PROCESSES_REMAIN",
            "message": "kill -KILL failed: operation not permitted",
            "agent_name": "agent-z",
            "host_id": str(host_id),
        }
    ]
    # PROCESSES_REMAIN maps to exit code 3.
    assert data["exit_code"] == 3


# =============================================================================
# Archive integration tests (require tmux for running agents)
# =============================================================================


@pytest.mark.tmux
def test_stop_host_routes_to_provider_stop_host(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    create_test_agent: Callable[..., str],
    temp_host_dir: Path,
) -> None:
    """``stop --stop-host`` dispatches to ``provider.stop_host``, not ``stop_agents``.

    The local provider advertises ``supports_shutdown_hosts`` but refuses
    the actual host stop (you cannot stop your own computer), so reaching
    that refusal proves the flag routed to ``stop_host`` -- a plain
    ``stop_agents`` call would have succeeded instead.
    """
    create_test_agent("stop-host-routing-agent", "sleep 300031")

    result = cli_runner.invoke(
        stop,
        ["stop-host-routing-agent", "--stop-host"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot stop the local host" in result.output


@pytest.mark.tmux
def test_stop_dry_run_does_not_stop_agent(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    create_test_agent: Callable[..., str],
    temp_host_dir: Path,
) -> None:
    """``stop --dry-run`` reports the agent that would be stopped but leaves it running."""
    create_test_agent("dry-run-agent", "sleep 300019")

    dry_result = cli_runner.invoke(
        stop,
        ["dry-run-agent", "--dry-run"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert dry_result.exit_code == 0
    assert "Would stop:" in dry_result.output
    assert "dry-run-agent" in dry_result.output
    # The dry run must not actually stop anything.
    assert "Stopped agent" not in dry_result.output

    # Proof the agent was left running: a real stop now finds and stops it.
    real_result = cli_runner.invoke(
        stop,
        ["dry-run-agent"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert real_result.exit_code == 0
    assert "Stopped agent: dry-run-agent" in real_result.output


@pytest.mark.tmux
def test_stop_archive_sets_archived_at_label(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    create_test_agent: Callable[..., str],
    temp_host_dir: Path,
) -> None:
    """stop --archive should stop the agent and set the archived_at label."""
    create_test_agent("archive-test-agent", "sleep 300018")

    result = cli_runner.invoke(
        stop,
        ["archive-test-agent", "--archive"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "Stopped agent: archive-test-agent" in result.output
    assert "Updated labels for agent archive-test-agent" in result.output

    # Verify the archived_at label was set by reading the agent's data.json
    agents_dir = temp_host_dir / "agents"
    agent_dirs = list(agents_dir.iterdir())
    assert len(agent_dirs) >= 1

    for agent_dir in agent_dirs:
        data_path = agent_dir / "data.json"
        if data_path.exists():
            data = json.loads(data_path.read_text())
            if data.get("name") == "archive-test-agent":
                assert "archived_at" in data.get("labels", {}), "archived_at label should be set"
                return

    raise AssertionError("Could not find archive-test-agent data.json")
