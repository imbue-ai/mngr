import json
from datetime import datetime
from datetime import timezone

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.snapshot import SnapshotCreateCliOptions
from imbue.mng.cli.snapshot import SnapshotDestroyCliOptions
from imbue.mng.cli.snapshot import SnapshotListCliOptions
from imbue.mng.cli.snapshot import _classify_mixed_identifiers
from imbue.mng.cli.snapshot import _emit_create_result
from imbue.mng.cli.snapshot import _emit_destroy_result
from imbue.mng.cli.snapshot import _emit_list_snapshots
from imbue.mng.cli.snapshot import snapshot
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.main import cli
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName

# =============================================================================
# Options classes tests
# =============================================================================


def test_snapshot_create_cli_options_fields() -> None:
    """Test SnapshotCreateCliOptions has required fields."""
    opts = SnapshotCreateCliOptions(
        identifiers=("agent1",),
        agent_list=("agent2",),
        hosts=("host1",),
        all_agents=False,
        name="my-snapshot",
        dry_run=True,
        on_error="continue",
        include=(),
        exclude=(),
        stdin=False,
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
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.identifiers == ("agent1",)
    assert opts.agent_list == ("agent2",)
    assert opts.hosts == ("host1",)
    assert opts.all_agents is False
    assert opts.name == "my-snapshot"
    assert opts.dry_run is True
    assert opts.on_error == "continue"


def test_snapshot_list_cli_options_fields() -> None:
    """Test SnapshotListCliOptions has required fields."""
    opts = SnapshotListCliOptions(
        identifiers=("agent1",),
        agent_list=(),
        hosts=("host1",),
        all_agents=False,
        limit=10,
        include=(),
        exclude=(),
        after=None,
        before=None,
        output_format="json",
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
    assert opts.identifiers == ("agent1",)
    assert opts.hosts == ("host1",)
    assert opts.limit == 10


def test_snapshot_destroy_cli_options_fields() -> None:
    """Test SnapshotDestroyCliOptions has required fields."""
    opts = SnapshotDestroyCliOptions(
        agents=("agent1",),
        agent_list=(),
        snapshots=("snap-123",),
        all_snapshots=False,
        force=True,
        dry_run=False,
        include=(),
        exclude=(),
        stdin=False,
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
    assert opts.snapshots == ("snap-123",)
    assert opts.force is True


# =============================================================================
# _SnapshotGroup default-to-create tests
# =============================================================================


def test_snapshot_bare_invocation_defaults_to_create(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running `mng snapshot` with no args should forward to `snapshot create`."""
    result = cli_runner.invoke(snapshot, [], obj=plugin_manager)
    # Should attempt to run create (which errors asking for an agent),
    # not show group help or say "Missing command".
    assert "Missing command" not in result.output
    assert "Commands:" not in result.output
    assert "Must specify at least one agent" in result.output


def test_snapshot_unrecognized_subcommand_forwards_to_create(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running `mng snapshot nonexistent` should forward to `snapshot create nonexistent`.

    The local provider only accepts "localhost" as a host name, so
    "nonexistent" fails with "not found". The key assertion is that it
    does NOT say "No such command".
    """
    result = cli_runner.invoke(snapshot, ["nonexistent"], obj=plugin_manager)
    assert "No such command" not in result.output
    assert "Agent or host not found: nonexistent" in result.output


def test_snapshot_explicit_create_still_works(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running `mng snapshot create --help` should still work.

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
    """Running `mng snapshot list` should NOT be forwarded to create.

    Must invoke through the root cli group for correct help key resolution.
    """
    result = cli_runner.invoke(cli, ["snapshot", "list", "--help"])
    assert result.exit_code == 0
    assert "List snapshots" in result.output


def test_snapshot_destroy_subcommand_not_forwarded(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Running `mng snapshot destroy` should NOT be forwarded to create.

    Must invoke through the root cli group for correct help key resolution.
    """
    result = cli_runner.invoke(cli, ["snapshot", "destroy", "--help"])
    assert result.exit_code == 0
    assert "Destroy snapshots" in result.output


# =============================================================================
# _classify_mixed_identifiers tests
# =============================================================================


def test_classify_mixed_identifiers_empty_input_returns_empty_lists(
    temp_mng_ctx: MngContext,
) -> None:
    """Empty identifier list returns two empty lists."""
    agent_ids, host_ids = _classify_mixed_identifiers([], temp_mng_ctx)
    assert agent_ids == []
    assert host_ids == []


def test_classify_mixed_identifiers_no_agents_treats_all_as_hosts(
    temp_mng_ctx: MngContext,
) -> None:
    """When no agents exist, all identifiers are classified as host identifiers."""
    agent_ids, host_ids = _classify_mixed_identifiers(["foo", "bar"], temp_mng_ctx)
    assert agent_ids == []
    assert host_ids == ["foo", "bar"]


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


# =============================================================================
# Options model instantiation tests
# =============================================================================


def test_snapshot_destroy_cli_options_can_be_instantiated() -> None:
    """Test SnapshotDestroyCliOptions can be instantiated with all fields."""
    opts = SnapshotDestroyCliOptions(
        agents=(),
        agent_list=(),
        snapshots=(),
        all_snapshots=True,
        force=False,
        dry_run=True,
        include=(),
        exclude=(),
        stdin=False,
        output_format="json",
        quiet=True,
        verbose=1,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.all_snapshots is True
    assert opts.force is False
    assert opts.dry_run is True
    assert opts.quiet is True
    assert opts.verbose == 1


def test_snapshot_list_cli_options_can_be_instantiated() -> None:
    """Test SnapshotListCliOptions can be instantiated with various field values."""
    opts = SnapshotListCliOptions(
        identifiers=("a1", "a2"),
        agent_list=("a3",),
        hosts=(),
        all_agents=True,
        limit=5,
        include=(),
        exclude=(),
        after=None,
        before=None,
        output_format="jsonl",
        quiet=False,
        verbose=2,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.identifiers == ("a1", "a2")
    assert opts.all_agents is True
    assert opts.limit == 5
    assert opts.verbose == 2


# =============================================================================
# _classify_mixed_identifiers edge cases
# =============================================================================


def test_classify_mixed_identifiers_single_unknown_identifier(
    temp_mng_ctx: MngContext,
) -> None:
    """A single unknown identifier is classified as a host identifier."""
    agent_ids, host_ids = _classify_mixed_identifiers(["some-host-id"], temp_mng_ctx)
    assert agent_ids == []
    assert host_ids == ["some-host-id"]


def test_classify_mixed_identifiers_multiple_unknown_identifiers(
    temp_mng_ctx: MngContext,
) -> None:
    """Multiple unknown identifiers are all classified as host identifiers."""
    agent_ids, host_ids = _classify_mixed_identifiers(["host-a", "host-b", "host-c"], temp_mng_ctx)
    assert agent_ids == []
    assert host_ids == ["host-a", "host-b", "host-c"]


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
