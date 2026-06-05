import json
from datetime import datetime
from datetime import timezone

import click
import pluggy
import pytest
from click.testing import CliRunner
from pydantic import BaseModel

from imbue.mngr.cli.snapshot import SnapshotCreateCliOptions
from imbue.mngr.cli.snapshot import SnapshotDestroyCliOptions
from imbue.mngr.cli.snapshot import SnapshotListCliOptions
from imbue.mngr.cli.snapshot import _bucketize_mixed_identifiers
from imbue.mngr.cli.snapshot import _emit_create_result
from imbue.mngr.cli.snapshot import _emit_destroy_dry_run
from imbue.mngr.cli.snapshot import _emit_destroy_result
from imbue.mngr.cli.snapshot import _emit_list_snapshots
from imbue.mngr.cli.snapshot import snapshot
from imbue.mngr.cli.snapshot import snapshot_create
from imbue.mngr.cli.snapshot import snapshot_destroy
from imbue.mngr.cli.snapshot import snapshot_list
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.interfaces.data_types import SnapshotInfo
from imbue.mngr.main import cli
from imbue.mngr.primitives import AgentAddress
from imbue.mngr.primitives import AgentName
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


@pytest.mark.parametrize(
    "command,options_class,flag,attr",
    [
        (snapshot_create, SnapshotCreateCliOptions, "--name", "name"),
        (snapshot_create, SnapshotCreateCliOptions, "--on-error", "on_error"),
        (snapshot_create, SnapshotCreateCliOptions, "--host", "hosts"),
        (snapshot_list, SnapshotListCliOptions, "--limit", "limit"),
        (snapshot_list, SnapshotListCliOptions, "--host", "hosts"),
        (snapshot_destroy, SnapshotDestroyCliOptions, "--snapshot", "snapshots"),
        (snapshot_destroy, SnapshotDestroyCliOptions, "--all-snapshots", "all_snapshots"),
        (snapshot_destroy, SnapshotDestroyCliOptions, "--force", "force"),
    ],
)
def test_snapshot_click_flags_map_to_cli_option_fields(
    command: click.Command,
    options_class: type[BaseModel],
    flag: str,
    attr: str,
) -> None:
    """Each snapshot subcommand flag must populate the matching options-class field.

    setup_command_context builds the options class from click params by name, so a
    flag whose click ``dest`` drifts from the model field name would silently fail
    to populate. Assert the flag's option exists, targets the expected field, and
    that the field is declared on the options class.
    """
    matching = [p for p in command.params if attr == p.name]
    assert len(matching) == 1, f"expected exactly one click param named {attr!r}"
    param = matching[0]
    assert flag in param.opts or flag in param.secondary_opts
    assert attr in options_class.model_fields


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
# _bucketize_mixed_identifiers tests
# =============================================================================


def test_bucketize_mixed_identifiers_empty_input_returns_empty_lists() -> None:
    """Empty identifier list returns two empty lists."""
    agent_addrs, host_addrs = _bucketize_mixed_identifiers([])
    assert agent_addrs == []
    assert host_addrs == []


def test_bucketize_mixed_identifiers_bare_names_are_agents() -> None:
    """Bare names parse as agents under text-only disambiguation."""
    agent_addrs, host_addrs = _bucketize_mixed_identifiers(["foo", "bar"])
    assert agent_addrs == [
        AgentAddress(agent=AgentName("foo")),
        AgentAddress(agent=AgentName("bar")),
    ]
    assert host_addrs == []


def test_bucketize_mixed_identifiers_at_prefix_is_host() -> None:
    """A leading ``@`` forces host parsing."""
    agent_addrs, host_addrs = _bucketize_mixed_identifiers(["@my-host", "@my-host.modal"])
    assert agent_addrs == []
    assert host_addrs == [
        HostAddress(host=HostName("my-host")),
        HostAddress(host=HostName("my-host"), provider=ProviderInstanceName("modal")),
    ]


def test_bucketize_mixed_identifiers_host_id_is_host() -> None:
    """A bare HostId is recognized as a host without the ``@`` prefix."""
    host_id = HostId.generate()
    agent_addrs, host_addrs = _bucketize_mixed_identifiers([str(host_id)])
    assert agent_addrs == []
    assert host_addrs == [HostAddress(host=host_id)]


def test_bucketize_mixed_identifiers_mix_of_agents_and_hosts() -> None:
    """Agent tokens and host tokens go to their respective buckets."""
    agent_addrs, host_addrs = _bucketize_mixed_identifiers(["my-agent", "@my-host", "other-agent"])
    assert agent_addrs == [
        AgentAddress(agent=AgentName("my-agent")),
        AgentAddress(agent=AgentName("other-agent")),
    ]
    assert host_addrs == [HostAddress(host=HostName("my-host"))]


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


def test_emit_destroy_dry_run_human(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_destroy_dry_run lists each snapshot that would be destroyed."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    snapshots_to_delete = [
        ("host-1", ProviderInstanceName("modal"), SnapshotId("snap-abc"), "before-refactor"),
    ]
    _emit_destroy_dry_run(snapshots_to_delete, output_opts=output_opts)
    out = capsys.readouterr().out
    assert "Would destroy 1 snapshot(s)" in out
    assert "snap-abc" in out
    assert "before-refactor" in out
    assert "host-1" in out


def test_emit_destroy_dry_run_json(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_destroy_dry_run emits a dry_run JSON payload without a destroy count key."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    snapshots_to_delete = [
        ("host-1", ProviderInstanceName("modal"), SnapshotId("snap-abc"), "before-refactor"),
    ]
    _emit_destroy_dry_run(snapshots_to_delete, output_opts=output_opts)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["dry_run"] is True
    assert parsed["count"] == 1
    assert parsed["snapshots"][0]["snapshot_id"] == "snap-abc"


def test_emit_destroy_dry_run_format_template(capsys: pytest.CaptureFixture[str]) -> None:
    """_emit_destroy_dry_run renders custom format templates."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{snapshot_id}\t{host_id}")
    snapshots_to_delete = [
        ("host-1", ProviderInstanceName("modal"), SnapshotId("snap-abc"), "before-refactor"),
    ]
    _emit_destroy_dry_run(snapshots_to_delete, output_opts=output_opts)
    assert capsys.readouterr().out.strip() == "snap-abc\thost-1"


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


def test_emit_list_snapshots_format_template_none_size_renders_dash(capsys: pytest.CaptureFixture[str]) -> None:
    """A None size_bytes renders as a standalone "-" in the size column.

    Uses the "{size}" format template (rather than the HUMAN table, whose
    "-" * 110 separator line would make a bare "-" substring trivially present)
    so the assertion pins exactly what the None-size column produces.
    """
    snap = _make_test_snapshot(size_bytes=None)
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN, format_template="{size}")
    _emit_list_snapshots([("host-1", snap)], output_opts)
    captured = capsys.readouterr()
    assert captured.out.strip() == "-"


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
