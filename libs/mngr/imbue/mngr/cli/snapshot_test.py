import json
from datetime import datetime
from datetime import timezone
from typing import Callable

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.address_params import parse_agent_or_host_addresses_or_raise
from imbue.mngr.cli.snapshot import SnapshotCreateCliOptions
from imbue.mngr.cli.snapshot import SnapshotDestroyCliOptions
from imbue.mngr.cli.snapshot import SnapshotListCliOptions
from imbue.mngr.cli.snapshot import _agent_identifiers_for_targets
from imbue.mngr.cli.snapshot import _check_create_future_options
from imbue.mngr.cli.snapshot import _check_list_future_options
from imbue.mngr.cli.snapshot import _emit_create_result
from imbue.mngr.cli.snapshot import _emit_destroy_dry_run
from imbue.mngr.cli.snapshot import _emit_destroy_result
from imbue.mngr.cli.snapshot import _emit_list_snapshots
from imbue.mngr.cli.snapshot import _required_providers_for_targets
from imbue.mngr.cli.snapshot import snapshot
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.main import cli
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostAddress
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.primitives import SnapshotId
from imbue.mngr.primitives import SnapshotName

# =============================================================================
# Options classes tests
# =============================================================================


def test_snapshot_create_cli_options_fields() -> None:
    """Test SnapshotCreateCliOptions has required fields."""
    opts = SnapshotCreateCliOptions(
        identifiers=("agent1", "@host1"),
        name="my-snapshot",
        on_error="continue",
        tag=(),
        description=None,
        restart_if_larger_than=None,
        pause_during=True,
        wait=True,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.identifiers == ("agent1", "@host1")
    assert opts.name == "my-snapshot"
    assert opts.on_error == "continue"


def test_snapshot_list_cli_options_fields() -> None:
    """Test SnapshotListCliOptions has required fields."""
    opts = SnapshotListCliOptions(
        identifiers=("agent1", "@host1"),
        limit=10,
        after=None,
        before=None,
        output_format="json",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.identifiers == ("agent1", "@host1")
    assert opts.limit == 10


def test_snapshot_destroy_cli_options_fields() -> None:
    """Test SnapshotDestroyCliOptions has required fields."""
    opts = SnapshotDestroyCliOptions(
        identifiers=("agent1",),
        snapshots=("snap-123",),
        all_snapshots=False,
        force=True,
        dry_run=False,
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.snapshots == ("snap-123",)
    assert opts.force is True
    assert opts.dry_run is False


# =============================================================================
# _SnapshotGroup default command tests
# =============================================================================


def test_snapshot_bare_invocation_shows_help(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running `mngr snapshot` with no args should show help (no default command)."""
    result = cli_runner.invoke(snapshot, [], obj=plugin_manager)
    assert "Commands:" in result.output or "Usage:" in result.output


def test_snapshot_unrecognized_subcommand_errors(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running `mngr snapshot nonexistent` with no default should error."""
    result = cli_runner.invoke(snapshot, ["nonexistent"], obj=plugin_manager)
    assert result.exit_code != 0
    assert "No such command" in result.output


def test_snapshot_explicit_create_still_works(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running `mngr snapshot create --help` should still work.

    Must invoke through the root cli group so that _build_help_key produces
    the correct qualified key ("snapshot.create") for metadata resolution.
    """
    result = cli_runner.invoke(cli, ["snapshot", "create", "--help"])
    assert result.exit_code == 0
    assert "Create a snapshot" in result.output


def test_snapshot_list_subcommand_not_forwarded(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running `mngr snapshot list` should NOT be forwarded to create.

    Must invoke through the root cli group for correct help key resolution.
    """
    result = cli_runner.invoke(cli, ["snapshot", "list", "--help"])
    assert result.exit_code == 0
    assert "List snapshots" in result.output


def test_snapshot_destroy_subcommand_not_forwarded(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running `mngr snapshot destroy` should NOT be forwarded to create.

    Must invoke through the root cli group for correct help key resolution.
    """
    result = cli_runner.invoke(cli, ["snapshot", "destroy", "--help"])
    assert result.exit_code == 0
    assert "Destroy snapshots" in result.output


# =============================================================================
# parse_agent_or_host_addresses_or_raise tests
# =============================================================================


def test_parse_agent_or_host_addresses_empty_input_returns_empty_list() -> None:
    """Empty identifier list returns an empty list."""
    assert parse_agent_or_host_addresses_or_raise([]) == []


def test_parse_agent_or_host_addresses_bare_names_are_agents() -> None:
    """Bare names parse as agents under text-only disambiguation."""
    assert parse_agent_or_host_addresses_or_raise(["foo", "bar"]) == [
        AgentAddress(agent=AgentName("foo")),
        AgentAddress(agent=AgentName("bar")),
    ]


def test_parse_agent_or_host_addresses_at_prefix_is_host() -> None:
    """A leading ``@`` forces host parsing."""
    assert parse_agent_or_host_addresses_or_raise(["@my-host", "@my-host.modal"]) == [
        HostAddress(host=HostName("my-host")),
        HostAddress(host=HostName("my-host"), provider=ProviderInstanceName("modal")),
    ]


def test_parse_agent_or_host_addresses_host_id_is_host() -> None:
    """A bare HostId is recognized as a host without the ``@`` prefix."""
    host_id = HostId.generate()
    assert parse_agent_or_host_addresses_or_raise([str(host_id)]) == [HostAddress(host=host_id)]


def test_parse_agent_or_host_addresses_mix_of_agents_and_hosts() -> None:
    """Agent tokens and host tokens are parsed into a single mixed list, preserving order."""
    assert parse_agent_or_host_addresses_or_raise(["my-agent", "@my-host", "other-agent"]) == [
        AgentAddress(agent=AgentName("my-agent")),
        HostAddress(host=HostName("my-host")),
        AgentAddress(agent=AgentName("other-agent")),
    ]


# =============================================================================
# _emit_create_result format template tests
# =============================================================================


def test_emit_create_result_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_create_result renders format templates for created snapshots."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{snapshot_id}")
    created = [
        {"snapshot_id": "snap-abc", "host_id": "host-1", "provider": "local", "agent_names": ["agent1"]},
        {"snapshot_id": "snap-def", "host_id": "host-2", "provider": "local", "agent_names": ["agent2", "agent3"]},
    ]
    _emit_create_result(created, errors=[], output_opts=output_opts)
    lines = capsys.readouterr().out.strip().split("\n")
    assert lines == ["snap-abc", "snap-def"]


def test_emit_create_result_format_template_agent_names(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_create_result format template renders agent_names as comma-separated."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{agent_names}")
    created = [
        {"snapshot_id": "snap-abc", "host_id": "host-1", "provider": "local", "agent_names": ["a1", "a2"]},
    ]
    _emit_create_result(created, errors=[], output_opts=output_opts)
    assert capsys.readouterr().out.strip() == "a1, a2"


# =============================================================================
# _emit_destroy_result format template tests
# =============================================================================


def test_emit_destroy_result_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_destroy_result renders format templates for destroyed snapshots."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{snapshot_id}\t{host_id}")
    destroyed = [
        {"snapshot_id": "snap-abc", "host_id": "host-1", "provider": "local"},
    ]
    _emit_destroy_result(destroyed, output_opts=output_opts)
    assert capsys.readouterr().out.strip() == "snap-abc\thost-1"


def _make_destroy_dry_run_entry(
    host_id: HostId,
) -> tuple[DiscoveredHost, SnapshotId, str]:
    return (
        DiscoveredHost(
            host_id=host_id,
            host_name=HostName("host-1"),
            provider_name=ProviderInstanceName("modal"),
        ),
        SnapshotId("snap-abc"),
        "before-refactor",
    )


def test_emit_destroy_dry_run_human(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_destroy_dry_run lists each snapshot that would be destroyed."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    host_id = HostId.generate()
    snapshots_to_delete = [_make_destroy_dry_run_entry(host_id)]
    _emit_destroy_dry_run(snapshots_to_delete, output_opts=output_opts)
    out = capsys.readouterr().out
    assert "Would destroy 1 snapshot(s)" in out
    assert "snap-abc" in out
    assert "before-refactor" in out
    assert str(host_id) in out


def test_emit_destroy_dry_run_json(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_destroy_dry_run emits a dry_run JSON payload without a destroy count key."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    host_id = HostId.generate()
    snapshots_to_delete = [_make_destroy_dry_run_entry(host_id)]
    _emit_destroy_dry_run(snapshots_to_delete, output_opts=output_opts)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["dry_run"] is True
    assert parsed["count"] == 1
    assert parsed["snapshots"][0]["snapshot_id"] == "snap-abc"
    assert parsed["snapshots"][0]["host_id"] == str(host_id)


def test_emit_destroy_dry_run_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_destroy_dry_run renders custom format templates."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{snapshot_id}\t{host_id}")
    host_id = HostId.generate()
    snapshots_to_delete = [_make_destroy_dry_run_entry(host_id)]
    _emit_destroy_dry_run(snapshots_to_delete, output_opts=output_opts)
    assert capsys.readouterr().out.strip() == f"snap-abc\t{host_id}"


# =============================================================================
# Options model instantiation tests
# =============================================================================


def test_snapshot_destroy_cli_options_can_be_instantiated() -> None:
    """Test SnapshotDestroyCliOptions can be instantiated with all fields."""
    opts = SnapshotDestroyCliOptions(
        identifiers=(),
        snapshots=(),
        all_snapshots=True,
        force=False,
        dry_run=False,
        output_format="json",
        quiet=True,
        verbose=1,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.all_snapshots is True
    assert opts.force is False
    assert opts.quiet is True
    assert opts.verbose == 1


def test_snapshot_list_cli_options_can_be_instantiated() -> None:
    """Test SnapshotListCliOptions can be instantiated with various field values."""
    opts = SnapshotListCliOptions(
        identifiers=("a1", "a2"),
        limit=5,
        after=None,
        before=None,
        output_format="jsonl",
        quiet=False,
        verbose=2,
        log_file=None,
        log_commands=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.identifiers == ("a1", "a2")
    assert opts.limit == 5
    assert opts.verbose == 2


# =============================================================================
# _emit_create_result output format tests (beyond format templates)
# =============================================================================


def test_emit_create_result_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_create_result emits JSON with snapshots_created."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    created = [
        {"snapshot_id": "snap-1", "host_id": "host-1", "provider": "local", "agent_names": ["a1"]},
    ]
    _emit_create_result(created, errors=[], output_opts=output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["snapshots_created"] == created
    assert data["count"] == 1
    assert "errors" not in data


def test_emit_create_result_json_format_with_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_create_result emits JSON with errors when present."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    created = [
        {"snapshot_id": "snap-1", "host_id": "host-1", "provider": "local", "agent_names": ["a1"]},
    ]
    errors = [{"host_id": "host-2", "error": "fail"}]
    _emit_create_result(created, errors=errors, output_opts=output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["errors"] == errors
    assert data["error_count"] == 1


def test_emit_create_result_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_create_result emits JSONL create_result event."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    created = [
        {"snapshot_id": "snap-1", "host_id": "host-1", "provider": "local", "agent_names": ["a1"]},
    ]
    _emit_create_result(created, errors=[], output_opts=output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "create_result"
    assert data["count"] == 1


def test_emit_create_result_human_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_create_result emits human-readable output."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    created = [
        {"snapshot_id": "snap-1", "host_id": "host-1", "provider": "local", "agent_names": ["a1"]},
        {"snapshot_id": "snap-2", "host_id": "host-2", "provider": "local", "agent_names": ["a2"]},
    ]
    _emit_create_result(created, errors=[], output_opts=output_opts)
    captured = capsys.readouterr()
    assert "Created 2 snapshot(s)" in captured.out


# =============================================================================
# _emit_list_snapshots output format tests
# =============================================================================


def _make_test_snapshot(
    snapshot_id: str = "snap-abc",
    name: str = "test-snapshot",
    size_bytes: int | None = 1024,
) -> SnapshotInfo:
    """Create a test SnapshotInfo."""
    return SnapshotInfo(
        id=SnapshotId(snapshot_id),
        name=SnapshotName(name),
        created_at=datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
        size_bytes=size_bytes,
    )


def test_emit_list_snapshots_human_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_list_snapshots prints 'No snapshots found' when empty."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_list_snapshots([], output_opts)
    captured = capsys.readouterr()
    assert "No snapshots found" in captured.out


def test_emit_list_snapshots_human_with_snapshots(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_list_snapshots prints a table with snapshot info."""
    snap = _make_test_snapshot()
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_list_snapshots([("host-1", snap)], output_opts)
    captured = capsys.readouterr()
    output = captured.out
    assert "snap-abc" in output
    assert "test-snapshot" in output
    assert "host-1" in output


def test_emit_list_snapshots_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_list_snapshots emits JSON with snapshots array."""
    snap = _make_test_snapshot()
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    _emit_list_snapshots([("host-1", snap)], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["count"] == 1
    assert data["snapshots"][0]["host_id"] == "host-1"


def test_emit_list_snapshots_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_list_snapshots emits JSONL events per snapshot."""
    snap = _make_test_snapshot()
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    _emit_list_snapshots([("host-1", snap)], output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "snapshot"
    assert data["host_id"] == "host-1"


def test_emit_list_snapshots_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_list_snapshots renders format templates."""
    snap = _make_test_snapshot()
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{id}\t{name}")
    _emit_list_snapshots([("host-1", snap)], output_opts)
    captured = capsys.readouterr()
    assert "snap-abc\ttest-snapshot" in captured.out


def test_emit_list_snapshots_human_with_none_size(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_list_snapshots handles None size_bytes correctly."""
    snap = _make_test_snapshot(size_bytes=None)
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _emit_list_snapshots([("host-1", snap)], output_opts)
    captured = capsys.readouterr()
    output = captured.out
    # size_bytes=None should display as "-"
    assert "-" in output


# =============================================================================
# _emit_destroy_result output format tests (beyond format templates)
# =============================================================================


def test_emit_destroy_result_json_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_destroy_result emits JSON with destroyed count."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    destroyed = [
        {"snapshot_id": "snap-1", "host_id": "host-1", "provider": "local"},
    ]
    _emit_destroy_result(destroyed, output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["count"] == 1
    assert data["snapshots_destroyed"] == destroyed


def test_emit_destroy_result_jsonl_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_destroy_result emits JSONL destroy_result event."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    destroyed = [
        {"snapshot_id": "snap-1", "host_id": "host-1", "provider": "local"},
    ]
    _emit_destroy_result(destroyed, output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "destroy_result"
    assert data["count"] == 1


def test_emit_destroy_result_human_format(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_destroy_result emits human-readable destroy message."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    destroyed = [
        {"snapshot_id": "snap-1", "host_id": "host-1", "provider": "local"},
        {"snapshot_id": "snap-2", "host_id": "host-2", "provider": "local"},
    ]
    _emit_destroy_result(destroyed, output_opts)
    captured = capsys.readouterr()
    assert "Destroyed 2 snapshot(s)" in captured.out


# =============================================================================
# Additional _emit_create_result tests (error paths)
# =============================================================================


def test_emit_create_result_jsonl_with_errors(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_create_result in JSONL format should include error count."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    created = [{"snapshot_id": "snap-1", "host_id": "host-1", "provider": "local", "agent_names": []}]
    errors = [{"host_id": "host-2", "error": "timeout"}]
    _emit_create_result(created, errors, output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "create_result"
    assert data["error_count"] == 1


def test_emit_list_snapshots_human_table_with_size(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_list_snapshots in HUMAN format should output table with size."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    snap = SnapshotInfo(
        id=SnapshotId("snap-list-table-1"),
        name=SnapshotName("my-snapshot"),
        created_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
        size_bytes=1048576,
    )
    _emit_list_snapshots([("host-abc", snap)], output_opts)
    captured = capsys.readouterr()
    output = captured.out
    assert "ID" in output
    assert "snap-list-table-1" in output
    assert "my-snapshot" in output
    assert "host-abc" in output


# =============================================================================
# Discovery-narrowing helper tests
# =============================================================================


def test__required_providers_for_targets_returns_none_for_unpinned_agent() -> None:
    """A bare agent address (no host/provider) disables provider narrowing."""
    assert _required_providers_for_targets([AgentAddress(agent=AgentName("a"))]) is None


def test__required_providers_for_targets_returns_none_for_unpinned_host() -> None:
    """A host address without a provider disables provider narrowing."""
    assert _required_providers_for_targets([HostAddress(host=HostName("h"))]) is None


def test__required_providers_for_targets_dedupes_pinned_providers() -> None:
    """When every address pins a provider, the deduped sorted tuple is returned."""
    addrs = [
        AgentAddress(
            agent=AgentName("a"),
            host=HostAddress(host=HostName("h1"), provider=ProviderInstanceName("modal")),
        ),
        HostAddress(host=HostName("h2"), provider=ProviderInstanceName("local")),
        HostAddress(host=HostName("h3"), provider=ProviderInstanceName("modal")),
    ]
    assert _required_providers_for_targets(addrs) == (
        ProviderInstanceName("local"),
        ProviderInstanceName("modal"),
    )


def test__required_providers_for_targets_empty_input_returns_none() -> None:
    assert _required_providers_for_targets([]) is None


def test__agent_identifiers_for_targets_returns_none_when_any_host_address_present() -> None:
    """A mixed list with any HostAddress disqualifies event-stream narrowing."""
    addrs = [
        AgentAddress(agent=AgentName("a")),
        HostAddress(host=HostName("h")),
    ]
    assert _agent_identifiers_for_targets(addrs) is None


def test__agent_identifiers_for_targets_collects_when_all_agents() -> None:
    """When every address is an AgentAddress, identifiers are collected in order."""
    addrs = [
        AgentAddress(agent=AgentName("a")),
        AgentAddress(agent=AgentName("b")),
    ]
    assert _agent_identifiers_for_targets(addrs) == ("a", "b")


def test__agent_identifiers_for_targets_empty_input_returns_none() -> None:
    assert _agent_identifiers_for_targets([]) is None


# =============================================================================
# Tests for [future] flag guards
# =============================================================================


def _make_snapshot_create_opts(
    identifiers: tuple[str, ...] = ("agent1",),
    name: str | None = None,
    on_error: str = "abort",
    tag: tuple[str, ...] = (),
    description: str | None = None,
    restart_if_larger_than: str | None = None,
    pause_during: bool = True,
    wait: bool = True,
    output_format: str = "human",
    quiet: bool = False,
    verbose: int = 0,
    log_file: str | None = None,
    log_commands: bool | None = None,
    plugin: tuple[str, ...] = (),
    disable_plugin: tuple[str, ...] = (),
) -> SnapshotCreateCliOptions:
    """Construct a SnapshotCreateCliOptions with safe defaults, allowing overrides."""
    return SnapshotCreateCliOptions(
        identifiers=identifiers,
        name=name,
        on_error=on_error,
        tag=tag,
        description=description,
        restart_if_larger_than=restart_if_larger_than,
        pause_during=pause_during,
        wait=wait,
        output_format=output_format,
        quiet=quiet,
        verbose=verbose,
        log_file=log_file,
        log_commands=log_commands,
        plugin=plugin,
        disable_plugin=disable_plugin,
    )


def _make_snapshot_list_opts(
    identifiers: tuple[str, ...] = ("agent1",),
    limit: int | None = 10,
    after: str | None = None,
    before: str | None = None,
    output_format: str = "json",
    quiet: bool = False,
    verbose: int = 0,
    log_file: str | None = None,
    log_commands: bool | None = None,
    plugin: tuple[str, ...] = (),
    disable_plugin: tuple[str, ...] = (),
) -> SnapshotListCliOptions:
    """Construct a SnapshotListCliOptions with safe defaults, allowing overrides."""
    return SnapshotListCliOptions(
        identifiers=identifiers,
        limit=limit,
        after=after,
        before=before,
        output_format=output_format,
        quiet=quiet,
        verbose=verbose,
        log_file=log_file,
        log_commands=log_commands,
        plugin=plugin,
        disable_plugin=disable_plugin,
    )


@pytest.mark.parametrize(
    ("flag", "build_opts"),
    [
        ("--tag", lambda: _make_snapshot_create_opts(tag=("k=v",))),
        ("--description", lambda: _make_snapshot_create_opts(description="anything")),
        ("--restart-if-larger-than", lambda: _make_snapshot_create_opts(restart_if_larger_than="5G")),
        ("--no-pause-during", lambda: _make_snapshot_create_opts(pause_during=False)),
        ("--no-wait", lambda: _make_snapshot_create_opts(wait=False)),
    ],
)
def test_snapshot_create_future_flags_raise_not_implemented_error(
    flag: str, build_opts: Callable[[], SnapshotCreateCliOptions]
) -> None:
    """`mngr snapshot create`'s `[future]` flags must still raise NotImplementedError.

    The synopsis at `mngr snapshot create`'s ``CommandHelpMetadata``
    intentionally omits these flags because they're unimplemented stubs.
    When a case below stops raising NotImplementedError, the flag has
    been implemented. Please:
        1. Add the flag to `mngr snapshot create`'s
           ``CommandHelpMetadata.synopsis`` in ``snapshot.py``.
        2. Drop the `[future]` suffix from the option's ``--help`` text.
        3. Remove the offending case from this test (and the matching
           branch in ``_check_create_future_options``).
    """
    with pytest.raises(NotImplementedError):
        _check_create_future_options(build_opts())


@pytest.mark.parametrize(
    ("flag", "build_opts"),
    [
        ("--after", lambda: _make_snapshot_list_opts(after="2026-01-01")),
        ("--before", lambda: _make_snapshot_list_opts(before="2026-01-01")),
    ],
)
def test_snapshot_list_future_flags_raise_not_implemented_error(
    flag: str, build_opts: Callable[[], SnapshotListCliOptions]
) -> None:
    """`mngr snapshot list`'s `[future]` flags must still raise NotImplementedError.

    The synopsis at `mngr snapshot list`'s ``CommandHelpMetadata``
    intentionally omits these flags because they're unimplemented stubs.
    When a case below stops raising NotImplementedError, the flag has
    been implemented. Please:
        1. Add the flag to `mngr snapshot list`'s
           ``CommandHelpMetadata.synopsis`` in ``snapshot.py``.
        2. Drop the `[future]` suffix from the option's ``--help`` text.
        3. Remove the offending case from this test (and the matching
           branch in ``_check_list_future_options``).
    """
    with pytest.raises(NotImplementedError):
        _check_list_future_options(build_opts())
