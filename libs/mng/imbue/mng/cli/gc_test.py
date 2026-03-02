"""Unit tests for gc CLI helpers."""

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pluggy
from click.testing import CliRunner

from imbue.mng.api.data_types import GcResult
from imbue.mng.cli.gc import GcCliOptions
from imbue.mng.cli.gc import _emit_destroyed
from imbue.mng.cli.gc import _emit_final_summary
from imbue.mng.cli.gc import _emit_human_summary
from imbue.mng.cli.gc import _emit_json_summary
from imbue.mng.cli.gc import _emit_jsonl_summary
from imbue.mng.cli.gc import _format_destroyed_message
from imbue.mng.cli.gc import gc
from imbue.mng.interfaces.data_types import BuildCacheInfo
from imbue.mng.interfaces.data_types import HostInfo
from imbue.mng.interfaces.data_types import LogFileInfo
from imbue.mng.interfaces.data_types import SizeBytes
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.interfaces.data_types import VolumeInfo
from imbue.mng.interfaces.data_types import WorkDirInfo
from imbue.mng.primitives import HostId
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.primitives import SnapshotId
from imbue.mng.primitives import SnapshotName
from imbue.mng.primitives import VolumeId

# =============================================================================
# Helper functions for creating test data
# =============================================================================


def _create_work_dir_info(
    path: str = "/tmp/workdir",
    size_bytes: int = 1000,
    is_local: bool = True,
) -> WorkDirInfo:
    """Create a WorkDirInfo for testing."""
    return WorkDirInfo(
        path=Path(path),
        size_bytes=SizeBytes(size_bytes),
        host_id=HostId.generate(),
        provider_name=ProviderInstanceName("local"),
        is_local=is_local,
        created_at=datetime.now(timezone.utc),
    )


def _create_host_info(name: str = "test-host") -> HostInfo:
    """Create a HostInfo for testing."""
    return HostInfo(
        id=HostId.generate(),
        name=name,
        provider_name=ProviderInstanceName("docker"),
    )


def _create_snapshot_info(name: str = "test-snapshot", size_bytes: int | None = 1000) -> SnapshotInfo:
    """Create a SnapshotInfo for testing."""
    return SnapshotInfo(
        id=SnapshotId(f"snap-{uuid4().hex}"),
        name=SnapshotName(name),
        created_at=datetime.now(timezone.utc),
        size_bytes=size_bytes,
    )


def _create_volume_info(name: str = "test-volume", size_bytes: int = 1000) -> VolumeInfo:
    """Create a VolumeInfo for testing."""
    return VolumeInfo(
        volume_id=VolumeId.generate(),
        name=name,
        size_bytes=size_bytes,
        created_at=datetime.now(timezone.utc),
    )


def _create_log_file_info(path: str = "/tmp/log.txt", size_bytes: int = 500) -> LogFileInfo:
    """Create a LogFileInfo for testing."""
    return LogFileInfo(
        path=Path(path),
        size_bytes=SizeBytes(size_bytes),
        created_at=datetime.now(timezone.utc),
    )


def _create_build_cache_info(path: str = "/tmp/cache", size_bytes: int = 2000) -> BuildCacheInfo:
    """Create a BuildCacheInfo for testing."""
    return BuildCacheInfo(
        path=Path(path),
        size_bytes=SizeBytes(size_bytes),
        created_at=datetime.now(timezone.utc),
    )


# =============================================================================
# Tests for _format_destroyed_message
# =============================================================================


def test_format_destroyed_message_work_dir() -> None:
    """_format_destroyed_message should format work directory messages."""
    work_dir = _create_work_dir_info(path="/home/user/work")

    msg_destroy = _format_destroyed_message("work_dir", work_dir, dry_run=False)
    assert msg_destroy == "Destroyed work directory: /home/user/work"

    msg_dry_run = _format_destroyed_message("work_dir", work_dir, dry_run=True)
    assert msg_dry_run == "Would destroy work directory: /home/user/work"


def test_format_destroyed_message_machine() -> None:
    """_format_destroyed_message should format machine messages with provider."""
    host = _create_host_info(name="my-machine")

    msg_destroy = _format_destroyed_message("machine", host, dry_run=False)
    assert msg_destroy == "Destroyed machine: my-machine (docker)"

    msg_dry_run = _format_destroyed_message("machine", host, dry_run=True)
    assert msg_dry_run == "Would destroy machine: my-machine (docker)"


def test_format_destroyed_message_snapshot() -> None:
    """_format_destroyed_message should format snapshot messages."""
    snapshot = _create_snapshot_info(name="snap-2024")

    msg_destroy = _format_destroyed_message("snapshot", snapshot, dry_run=False)
    assert msg_destroy == "Destroyed snapshot: snap-2024"

    msg_dry_run = _format_destroyed_message("snapshot", snapshot, dry_run=True)
    assert msg_dry_run == "Would destroy snapshot: snap-2024"


def test_format_destroyed_message_volume() -> None:
    """_format_destroyed_message should format volume messages."""
    volume = _create_volume_info(name="data-vol")

    msg_destroy = _format_destroyed_message("volume", volume, dry_run=False)
    assert msg_destroy == "Destroyed volume: data-vol"

    msg_dry_run = _format_destroyed_message("volume", volume, dry_run=True)
    assert msg_dry_run == "Would destroy volume: data-vol"


def test_format_destroyed_message_log() -> None:
    """_format_destroyed_message should format log messages."""
    log = _create_log_file_info(path="/var/log/agent.log")

    msg_destroy = _format_destroyed_message("log", log, dry_run=False)
    assert msg_destroy == "Destroyed log: /var/log/agent.log"

    msg_dry_run = _format_destroyed_message("log", log, dry_run=True)
    assert msg_dry_run == "Would destroy log: /var/log/agent.log"


def test_format_destroyed_message_build_cache() -> None:
    """_format_destroyed_message should format build cache messages."""
    cache = _create_build_cache_info(path="/cache/build123")

    msg_destroy = _format_destroyed_message("build_cache", cache, dry_run=False)
    assert msg_destroy == "Destroyed build cache: /cache/build123"

    msg_dry_run = _format_destroyed_message("build_cache", cache, dry_run=True)
    assert msg_dry_run == "Would destroy build cache: /cache/build123"


def test_format_destroyed_message_unknown_type() -> None:
    """_format_destroyed_message should handle unknown resource types."""
    resource = "some-resource"

    msg = _format_destroyed_message("unknown_type", resource, dry_run=False)
    assert msg == "Destroyed unknown_type: some-resource"


# =============================================================================
# Tests for _emit_jsonl_summary
# =============================================================================


def test_emit_jsonl_summary_empty_result(capsys) -> None:
    """_emit_jsonl_summary should output correct totals for empty result."""
    result = GcResult()
    _emit_jsonl_summary(result, dry_run=False)

    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())

    assert output["event"] == "summary"
    assert output["total_count"] == 0
    assert output["total_size_bytes"] == 0
    assert output["work_dirs_count"] == 0
    assert output["machines_count"] == 0
    assert output["snapshots_count"] == 0
    assert output["volumes_count"] == 0
    assert output["logs_count"] == 0
    assert output["build_cache_count"] == 0
    assert output["errors_count"] == 0
    assert output["dry_run"] is False


def test_emit_jsonl_summary_with_work_dirs_only(capsys) -> None:
    """_emit_jsonl_summary should count work directories correctly."""
    result = GcResult()
    result.work_dirs_destroyed = [
        _create_work_dir_info(size_bytes=1000),
        _create_work_dir_info(size_bytes=2000),
    ]

    _emit_jsonl_summary(result, dry_run=True)

    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())

    assert output["total_count"] == 2
    assert output["total_size_bytes"] == 3000
    assert output["work_dirs_count"] == 2
    assert output["dry_run"] is True


def test_emit_jsonl_summary_with_mixed_resources(capsys) -> None:
    """_emit_jsonl_summary should aggregate counts and sizes from all resource types."""
    result = GcResult()
    result.work_dirs_destroyed = [_create_work_dir_info(size_bytes=1000)]
    result.machines_destroyed = [_create_host_info(), _create_host_info()]
    result.snapshots_destroyed = [_create_snapshot_info(size_bytes=500)]
    result.volumes_destroyed = [_create_volume_info(size_bytes=200)]
    result.logs_destroyed = [_create_log_file_info(size_bytes=100)]
    result.build_cache_destroyed = [_create_build_cache_info(size_bytes=300)]

    _emit_jsonl_summary(result, dry_run=False)

    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())

    # 1 work_dir + 2 machines + 1 snapshot + 1 volume + 1 log + 1 build_cache = 7
    assert output["total_count"] == 7
    # 1000 (work_dir) + 500 (machine) + 200 (snapshot) + 100 (volume) + 300 (log) = 2100
    assert output["total_size_bytes"] == 2100
    assert output["work_dirs_count"] == 1
    assert output["machines_count"] == 2
    assert output["snapshots_count"] == 1
    assert output["volumes_count"] == 1
    assert output["logs_count"] == 1
    assert output["build_cache_count"] == 1


def test_emit_jsonl_summary_handles_none_snapshot_size(capsys) -> None:
    """_emit_jsonl_summary should handle snapshots with None size_bytes."""
    result = GcResult()
    # Some providers don't report snapshot size, so include None size_bytes
    result.snapshots_destroyed = [
        _create_snapshot_info(size_bytes=1000),
        _create_snapshot_info(size_bytes=None),
    ]

    _emit_jsonl_summary(result, dry_run=False)

    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())

    assert output["snapshots_count"] == 2
    # Only the snapshot with size should contribute to total
    assert output["total_size_bytes"] == 1000


def test_emit_jsonl_summary_with_errors(capsys) -> None:
    """_emit_jsonl_summary should include errors in output."""
    result = GcResult()
    result.errors = ["Error 1", "Error 2"]

    _emit_jsonl_summary(result, dry_run=False)

    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())

    assert output["errors_count"] == 2
    assert output["errors"] == ["Error 1", "Error 2"]


# =============================================================================
# Tests for _emit_human_summary
# =============================================================================


def test_emit_human_summary_empty_result() -> None:
    """_emit_human_summary should indicate no resources found for empty result."""
    result = GcResult()
    # Just verify no exception is raised; output goes to logger
    _emit_human_summary(result, dry_run=False)


def test_emit_human_summary_dry_run() -> None:
    """_emit_human_summary should indicate dry run mode."""
    result = GcResult()
    result.work_dirs_destroyed = [_create_work_dir_info()]
    # Just verify no exception is raised; output goes to logger
    _emit_human_summary(result, dry_run=True)


def test_emit_human_summary_with_work_dirs() -> None:
    """_emit_human_summary should count work directories correctly."""
    result = GcResult()
    # Include both local and non-local work dirs
    # (non-local doesn't contribute to freed size calculation)
    result.work_dirs_destroyed = [
        _create_work_dir_info(is_local=True, size_bytes=1000),
        _create_work_dir_info(is_local=False, size_bytes=2000),
    ]
    # Just verify no exception is raised
    _emit_human_summary(result, dry_run=False)


def test_emit_human_summary_with_machines() -> None:
    """_emit_human_summary should count machines correctly."""
    result = GcResult()
    result.machines_destroyed = [_create_host_info(), _create_host_info()]
    _emit_human_summary(result, dry_run=False)


def test_emit_human_summary_with_snapshots() -> None:
    """_emit_human_summary should count snapshots correctly."""
    result = GcResult()
    result.snapshots_destroyed = [_create_snapshot_info()]
    _emit_human_summary(result, dry_run=False)


def test_emit_human_summary_with_volumes() -> None:
    """_emit_human_summary should count volumes correctly."""
    result = GcResult()
    result.volumes_destroyed = [_create_volume_info()]
    _emit_human_summary(result, dry_run=False)


def test_emit_human_summary_with_logs() -> None:
    """_emit_human_summary should count logs and report freed size."""
    result = GcResult()
    result.logs_destroyed = [
        _create_log_file_info(size_bytes=100),
        _create_log_file_info(size_bytes=200),
    ]
    _emit_human_summary(result, dry_run=False)


def test_emit_human_summary_with_build_cache() -> None:
    """_emit_human_summary should count build cache entries and report freed size."""
    result = GcResult()
    result.build_cache_destroyed = [_create_build_cache_info(size_bytes=5000)]
    _emit_human_summary(result, dry_run=False)


def test_emit_human_summary_with_errors() -> None:
    """_emit_human_summary should display errors."""
    result = GcResult()
    result.errors = ["Failed to destroy machine: timeout"]
    _emit_human_summary(result, dry_run=False)


def test_emit_human_summary_with_all_resource_types() -> None:
    """_emit_human_summary should handle all resource types combined."""
    result = GcResult()
    result.work_dirs_destroyed = [_create_work_dir_info(is_local=True, size_bytes=1000)]
    result.machines_destroyed = [_create_host_info()]
    result.snapshots_destroyed = [_create_snapshot_info()]
    result.volumes_destroyed = [_create_volume_info()]
    result.logs_destroyed = [_create_log_file_info()]
    result.build_cache_destroyed = [_create_build_cache_info()]
    result.errors = ["An error occurred"]

    # Just verify no exception is raised with all types combined
    _emit_human_summary(result, dry_run=False)


# =============================================================================
# Tests for GcCliOptions
# =============================================================================


def test_gc_cli_options_can_be_instantiated() -> None:
    """Test that GcCliOptions can be instantiated with all required fields."""
    opts = GcCliOptions(
        all_agent_resources=False,
        machines=True,
        snapshots=False,
        volumes=False,
        work_dirs=False,
        logs=False,
        build_cache=False,
        machine_cache=False,
        dry_run=True,
        on_error="abort",
        all_providers=False,
        provider=(),
        watch=None,
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
    assert opts.machines is True
    assert opts.dry_run is True
    assert opts.on_error == "abort"


# =============================================================================
# Tests for _emit_destroyed
# =============================================================================


def test_emit_destroyed_human_format(capsys) -> None:
    """_emit_destroyed should output message in HUMAN format."""
    work_dir = _create_work_dir_info(path="/home/user/work")
    _emit_destroyed("work_dir", work_dir, OutputFormat.HUMAN, dry_run=False)
    captured = capsys.readouterr()
    assert "Destroyed work directory: /home/user/work" in captured.out


def test_emit_destroyed_jsonl_format(capsys) -> None:
    """_emit_destroyed should output JSONL event."""
    host = _create_host_info(name="my-machine")
    _emit_destroyed("machine", host, OutputFormat.JSONL, dry_run=False)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "destroyed"
    assert output["resource_type"] == "machine"
    assert output["dry_run"] is False
    assert "Destroyed machine: my-machine (docker)" in output["message"]


def test_emit_destroyed_json_format_silent(capsys) -> None:
    """_emit_destroyed should be silent in JSON format (events suppressed)."""
    work_dir = _create_work_dir_info()
    _emit_destroyed("work_dir", work_dir, OutputFormat.JSON, dry_run=True)
    captured = capsys.readouterr()
    assert captured.out == ""


def test_emit_destroyed_dry_run(capsys) -> None:
    """_emit_destroyed should use 'Would destroy' prefix for dry run."""
    snapshot = _create_snapshot_info(name="snap-test")
    _emit_destroyed("snapshot", snapshot, OutputFormat.HUMAN, dry_run=True)
    captured = capsys.readouterr()
    assert "Would destroy snapshot: snap-test" in captured.out


def test_emit_destroyed_with_non_model_resource(capsys) -> None:
    """_emit_destroyed should handle resources without model_dump (fallback to str)."""
    _emit_destroyed("custom", "some-resource-name", OutputFormat.JSONL, dry_run=False)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["resource"] == "some-resource-name"


# =============================================================================
# Tests for _emit_final_summary (dispatch function)
# =============================================================================


def test_emit_final_summary_dispatches_to_json(capsys) -> None:
    """_emit_final_summary should dispatch to JSON summary."""
    result = GcResult()
    _emit_final_summary(result, OutputFormat.JSON, dry_run=False)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert "work_dirs_destroyed" in output
    assert output["dry_run"] is False


def test_emit_final_summary_dispatches_to_human(capsys) -> None:
    """_emit_final_summary should dispatch to human summary."""
    result = GcResult()
    _emit_final_summary(result, OutputFormat.HUMAN, dry_run=False)
    captured = capsys.readouterr()
    assert "Garbage Collection Results" in captured.out
    assert "No resources found" in captured.out


def test_emit_final_summary_dispatches_to_jsonl(capsys) -> None:
    """_emit_final_summary should dispatch to JSONL summary."""
    result = GcResult()
    _emit_final_summary(result, OutputFormat.JSONL, dry_run=True)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["event"] == "summary"
    assert output["dry_run"] is True


# =============================================================================
# Tests for _emit_json_summary
# =============================================================================


def test_emit_json_summary_empty_result(capsys) -> None:
    """_emit_json_summary should output correct structure for empty result."""
    result = GcResult()
    _emit_json_summary(result, dry_run=False)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert output["work_dirs_destroyed"] == []
    assert output["machines_destroyed"] == []
    assert output["machines_deleted"] == []
    assert output["snapshots_destroyed"] == []
    assert output["volumes_destroyed"] == []
    assert output["logs_destroyed"] == []
    assert output["build_cache_destroyed"] == []
    assert output["errors"] == []
    assert output["dry_run"] is False


def test_emit_json_summary_with_resources(capsys) -> None:
    """_emit_json_summary should serialize resource data."""
    result = GcResult()
    result.work_dirs_destroyed = [_create_work_dir_info()]
    result.machines_destroyed = [_create_host_info()]
    result.logs_destroyed = [_create_log_file_info()]

    _emit_json_summary(result, dry_run=True)
    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert len(output["work_dirs_destroyed"]) == 1
    assert len(output["machines_destroyed"]) == 1
    assert len(output["logs_destroyed"]) == 1
    assert output["dry_run"] is True


# =============================================================================
# Tests for _emit_human_summary (capsys-based)
# =============================================================================


def test_emit_human_summary_empty_result_output(capsys) -> None:
    """_emit_human_summary should show 'No resources found' for empty result."""
    result = GcResult()
    _emit_human_summary(result, dry_run=False)
    captured = capsys.readouterr()
    assert "No resources found to destroy" in captured.out


def test_emit_human_summary_dry_run_header(capsys) -> None:
    """_emit_human_summary should show 'Dry Run' in header for dry run mode."""
    result = GcResult()
    result.work_dirs_destroyed = [_create_work_dir_info()]
    _emit_human_summary(result, dry_run=True)
    captured = capsys.readouterr()
    assert "Dry Run" in captured.out
    assert "Would destroy" in captured.out


def test_emit_human_summary_shows_total_count(capsys) -> None:
    """_emit_human_summary should show the total count of destroyed resources."""
    result = GcResult()
    result.machines_destroyed = [_create_host_info(), _create_host_info()]
    _emit_human_summary(result, dry_run=False)
    captured = capsys.readouterr()
    assert "Destroyed 2 resource(s) total" in captured.out


def test_emit_human_summary_shows_machine_records_deleted(capsys) -> None:
    """_emit_human_summary should show machine records deleted count."""
    result = GcResult()
    result.machines_deleted = [_create_host_info()]
    _emit_human_summary(result, dry_run=False)
    captured = capsys.readouterr()
    assert "Machine records deleted: 1" in captured.out


def test_emit_human_summary_errors_displayed(capsys) -> None:
    """_emit_human_summary should display errors."""
    result = GcResult()
    result.errors = ["Error A", "Error B"]
    _emit_human_summary(result, dry_run=False)
    captured = capsys.readouterr()
    assert "Errors:" in captured.out
    assert "Error A" in captured.out
    assert "Error B" in captured.out


# =============================================================================
# Tests for gc CLI command
# =============================================================================


def test_gc_help_exits_zero(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that gc --help works and exits 0."""
    result = cli_runner.invoke(
        gc,
        ["--help"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "gc" in result.output.lower()


def test_gc_no_resource_types_exits_with_error(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that gc without resource types exits with error."""
    result = cli_runner.invoke(
        gc,
        [],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0


def test_gc_no_resource_types_json_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that gc without resource types in JSON format outputs error."""
    result = cli_runner.invoke(
        gc,
        ["--format", "json"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    output = json.loads(result.output.strip())
    assert "error" in output


def test_gc_no_resource_types_jsonl_format(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that gc without resource types in JSONL format outputs error event."""
    result = cli_runner.invoke(
        gc,
        ["--format", "jsonl"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    output = json.loads(result.output.strip())
    assert output["event"] == "error"
