"""Unit tests for local deploy.py."""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mng.config.data_types import MngContext
from imbue.mng_schedule.data_types import ScheduleCreationRecord
from imbue.mng_schedule.data_types import ScheduleTriggerDefinition
from imbue.mng_schedule.data_types import ScheduledMngCommand
from imbue.mng_schedule.implementations.local.deploy import _save_creation_record
from imbue.mng_schedule.implementations.local.deploy import _stage_env_file
from imbue.mng_schedule.implementations.local.deploy import build_wrapper_script
from imbue.mng_schedule.implementations.local.deploy import deploy_local_schedule
from imbue.mng_schedule.implementations.local.deploy import list_local_schedule_creation_records


def _make_test_trigger(name: str = "test-trigger") -> ScheduleTriggerDefinition:
    return ScheduleTriggerDefinition(
        name=name,
        command=ScheduledMngCommand.CREATE,
        args="--message hello",
        schedule_cron="0 2 * * *",
        provider="local",
    )


# =============================================================================
# build_wrapper_script tests
# =============================================================================


def test_build_wrapper_script_contains_path() -> None:
    trigger = _make_test_trigger()
    script = build_wrapper_script(
        trigger=trigger,
        working_directory="/home/user/project",
        path_value="/usr/local/bin:/usr/bin",
        env_file_path=None,
    )
    assert "export PATH=" in script
    assert "/usr/local/bin:/usr/bin" in script


def test_build_wrapper_script_contains_cd_to_working_dir() -> None:
    trigger = _make_test_trigger()
    script = build_wrapper_script(
        trigger=trigger,
        working_directory="/home/user/project",
        path_value="/usr/bin",
        env_file_path=None,
    )
    assert "cd " in script
    assert "/home/user/project" in script


def test_build_wrapper_script_contains_mng_command_and_args() -> None:
    trigger = ScheduleTriggerDefinition(
        name="test",
        command=ScheduledMngCommand.CREATE,
        args="--type claude --message 'do work'",
        schedule_cron="0 2 * * *",
        provider="local",
    )
    script = build_wrapper_script(
        trigger=trigger,
        working_directory="/tmp",
        path_value="/usr/bin",
        env_file_path=None,
    )
    assert "uv run mng create" in script
    assert "do work" in script


def test_build_wrapper_script_includes_env_sourcing() -> None:
    trigger = _make_test_trigger()
    script = build_wrapper_script(
        trigger=trigger,
        working_directory="/tmp",
        path_value="/usr/bin",
        env_file_path=Path("/home/user/.mng/schedule/test/.env"),
    )
    assert "source" in script
    assert ".env" in script


def test_build_wrapper_script_omits_env_when_none() -> None:
    trigger = _make_test_trigger()
    script = build_wrapper_script(
        trigger=trigger,
        working_directory="/tmp",
        path_value="/usr/bin",
        env_file_path=None,
    )
    assert "source" not in script


def test_build_wrapper_script_starts_with_shebang() -> None:
    trigger = _make_test_trigger()
    script = build_wrapper_script(
        trigger=trigger,
        working_directory="/tmp",
        path_value="/usr/bin",
        env_file_path=None,
    )
    assert script.startswith("#!/usr/bin/env bash\n")


def test_build_wrapper_script_uses_exec() -> None:
    trigger = _make_test_trigger()
    script = build_wrapper_script(
        trigger=trigger,
        working_directory="/tmp",
        path_value="/usr/bin",
        env_file_path=None,
    )
    assert "exec uv run mng" in script


def test_build_wrapper_script_empty_args() -> None:
    trigger = ScheduleTriggerDefinition(
        name="test",
        command=ScheduledMngCommand.EXEC,
        args="",
        schedule_cron="0 2 * * *",
        provider="local",
    )
    script = build_wrapper_script(
        trigger=trigger,
        working_directory="/tmp",
        path_value="/usr/bin",
        env_file_path=None,
    )
    assert "exec uv run mng exec" in script


# =============================================================================
# _stage_env_file tests
# =============================================================================


def test_stage_env_file_returns_none_when_no_vars(tmp_path: Path) -> None:
    result = _stage_env_file(tmp_path, pass_env=(), env_files=())
    assert result is None


def test_stage_env_file_writes_pass_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MY_VAR", "my_value")
    result = _stage_env_file(tmp_path, pass_env=["MY_VAR"], env_files=())
    assert result is not None
    assert result.exists()
    assert "MY_VAR=my_value" in result.read_text()


def test_stage_env_file_includes_env_files(tmp_path: Path) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("CUSTOM_KEY=custom_val\n")
    trigger_dir = tmp_path / "trigger"
    trigger_dir.mkdir()
    result = _stage_env_file(trigger_dir, pass_env=(), env_files=[env_file])
    assert result is not None
    assert "CUSTOM_KEY=custom_val" in result.read_text()


def test_stage_env_file_skips_missing_env_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
    result = _stage_env_file(tmp_path, pass_env=["NONEXISTENT_VAR"], env_files=())
    assert result is None


# =============================================================================
# deploy_local_schedule integration tests (with injected crontab/git stubs)
# =============================================================================


def test_deploy_local_schedule_creates_files_and_record(
    tmp_path: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """Test the full deploy flow with injected crontab and git hash stubs."""
    captured_crontab: list[str] = []

    trigger = _make_test_trigger()
    deploy_local_schedule(
        trigger,
        temp_mng_ctx,
        sys_argv=["mng", "schedule", "add"],
        crontab_reader=lambda: "",
        crontab_writer=captured_crontab.append,
        git_hash_resolver=lambda: "fakehash123",
    )

    # Verify crontab was written with the trigger
    assert len(captured_crontab) == 1
    assert "schedule:test-trigger" in captured_crontab[0]
    assert "0 2 * * *" in captured_crontab[0]

    # Verify wrapper script was created
    wrapper_script = tmp_path / ".mng" / "schedule" / "triggers" / "test-trigger" / "run.sh"
    assert wrapper_script.exists()
    assert wrapper_script.stat().st_mode & 0o100  # executable

    # Verify creation record was saved
    records = list_local_schedule_creation_records(temp_mng_ctx)
    assert len(records) == 1
    assert records[0].trigger.name == "test-trigger"
    assert records[0].mng_git_hash == "fakehash123"


def test_deploy_local_schedule_with_env_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    temp_mng_ctx: MngContext,
) -> None:
    """Test that pass-env vars are included in the wrapper script."""
    monkeypatch.setenv("MY_API_KEY", "sk-test-123")

    trigger = _make_test_trigger()
    deploy_local_schedule(
        trigger,
        temp_mng_ctx,
        pass_env=["MY_API_KEY"],
        crontab_reader=lambda: "",
        crontab_writer=lambda content: None,
        git_hash_resolver=lambda: "fakehash",
    )

    # Verify env file was created
    env_file = tmp_path / ".mng" / "schedule" / "triggers" / "test-trigger" / ".env"
    assert env_file.exists()
    assert "MY_API_KEY=sk-test-123" in env_file.read_text()

    # Verify wrapper script sources the env file
    wrapper = tmp_path / ".mng" / "schedule" / "triggers" / "test-trigger" / "run.sh"
    assert "source" in wrapper.read_text()


def test_deploy_local_schedule_update_replaces_crontab_entry(
    temp_mng_ctx: MngContext,
) -> None:
    """Test that deploying the same trigger name replaces the crontab entry."""
    captured_crontab: list[str] = []
    call_count = {"read": 0}

    def crontab_reader() -> str:
        call_count["read"] += 1
        if call_count["read"] == 1:
            return ""
        return captured_crontab[-1] if captured_crontab else ""

    # Deploy first time
    trigger1 = ScheduleTriggerDefinition(
        name="my-trigger",
        command=ScheduledMngCommand.CREATE,
        args="--message first",
        schedule_cron="0 1 * * *",
        provider="local",
    )
    deploy_local_schedule(
        trigger1,
        temp_mng_ctx,
        crontab_reader=crontab_reader,
        crontab_writer=captured_crontab.append,
        git_hash_resolver=lambda: "fakehash",
    )

    # Deploy second time with different schedule
    trigger2 = ScheduleTriggerDefinition(
        name="my-trigger",
        command=ScheduledMngCommand.CREATE,
        args="--message second",
        schedule_cron="0 3 * * *",
        provider="local",
    )
    deploy_local_schedule(
        trigger2,
        temp_mng_ctx,
        crontab_reader=crontab_reader,
        crontab_writer=captured_crontab.append,
        git_hash_resolver=lambda: "fakehash",
    )

    # Only the latest schedule should be in crontab
    final_crontab = captured_crontab[-1]
    assert final_crontab.count("schedule:my-trigger") == 1
    assert "0 3 * * *" in final_crontab


# =============================================================================
# list_local_schedule_creation_records tests
# =============================================================================


def test_list_local_schedule_creation_records_empty(
    temp_mng_ctx: MngContext,
) -> None:
    records = list_local_schedule_creation_records(temp_mng_ctx)
    assert records == []


def test_list_local_schedule_creation_records_round_trip(
    temp_mng_ctx: MngContext,
) -> None:
    """Test that saved records can be read back."""
    trigger = _make_test_trigger("my-schedule")
    record = ScheduleCreationRecord(
        trigger=trigger,
        full_commandline="mng schedule add ...",
        hostname="testhost",
        working_directory="/tmp/test",
        mng_git_hash="abc123",
        created_at=datetime(2025, 6, 15, 10, 0, 0, tzinfo=timezone.utc),
    )
    _save_creation_record(record, temp_mng_ctx)

    records = list_local_schedule_creation_records(temp_mng_ctx)
    assert len(records) == 1
    assert records[0].trigger.name == "my-schedule"
    assert records[0].hostname == "testhost"
    assert records[0].working_directory == "/tmp/test"


def test_list_local_schedule_creation_records_multiple(
    temp_mng_ctx: MngContext,
) -> None:
    """Test listing multiple records."""
    for name in ["alpha", "beta", "gamma"]:
        trigger = _make_test_trigger(name)
        record = ScheduleCreationRecord(
            trigger=trigger,
            full_commandline=f"mng schedule add --name {name}",
            hostname="testhost",
            working_directory="/tmp/test",
            mng_git_hash="abc123",
            created_at=datetime(2025, 6, 15, 10, 0, 0, tzinfo=timezone.utc),
        )
        _save_creation_record(record, temp_mng_ctx)

    records = list_local_schedule_creation_records(temp_mng_ctx)
    assert len(records) == 3
    names = [r.trigger.name for r in records]
    assert "alpha" in names
    assert "beta" in names
    assert "gamma" in names


def test_list_local_schedule_creation_records_skips_invalid_json(
    tmp_path: Path,
    temp_mng_ctx: MngContext,
) -> None:
    """Test that invalid JSON files are skipped with a warning."""
    records_dir = tmp_path / ".mng" / "schedule" / "records"
    records_dir.mkdir(parents=True)
    (records_dir / "bad.json").write_text("not valid json")

    records = list_local_schedule_creation_records(temp_mng_ctx)
    assert records == []


# =============================================================================
# Integration test: deploy then list round-trip
# =============================================================================


def test_deploy_then_list_round_trip_preserves_all_fields(
    temp_mng_ctx: MngContext,
) -> None:
    """Test that deploying a schedule and then listing it preserves all record fields."""
    trigger = ScheduleTriggerDefinition(
        name="integration-test",
        command=ScheduledMngCommand.CREATE,
        args="--type claude --message 'hello world'",
        schedule_cron="30 3 * * 1-5",
        provider="local",
        is_enabled=True,
    )

    deploy_local_schedule(
        trigger,
        temp_mng_ctx,
        sys_argv=["uv", "run", "mng", "schedule", "add", "--provider", "local"],
        crontab_reader=lambda: "",
        crontab_writer=lambda _: None,
        git_hash_resolver=lambda: "mng-hash-789",
    )

    records = list_local_schedule_creation_records(temp_mng_ctx)
    assert len(records) == 1

    record = records[0]
    assert record.trigger.name == "integration-test"
    assert record.trigger.command == ScheduledMngCommand.CREATE
    assert record.trigger.args == "--type claude --message 'hello world'"
    assert record.trigger.schedule_cron == "30 3 * * 1-5"
    assert record.trigger.provider == "local"
    assert record.trigger.is_enabled is True
    assert record.trigger.git_image_hash == ""
    assert record.mng_git_hash == "mng-hash-789"
    assert record.hostname != ""
    assert record.working_directory != ""
    assert "uv run mng schedule add" in record.full_commandline


# =============================================================================
# list_local_schedule_creation_records edge cases
# =============================================================================


def test_list_local_schedule_creation_records_skips_non_json_files(
    temp_mng_ctx: MngContext,
) -> None:
    """list_local_schedule_creation_records should skip non-JSON files."""
    trigger = _make_test_trigger("with-non-json")
    deploy_local_schedule(
        trigger,
        temp_mng_ctx,
        crontab_reader=lambda: "",
        crontab_writer=lambda _: None,
        git_hash_resolver=lambda: "hash",
    )

    # Create a non-json file in the records directory
    records_dir = temp_mng_ctx.config.default_host_dir.expanduser() / "schedule" / "records"
    (records_dir / "README.txt").write_text("not a record")

    records = list_local_schedule_creation_records(temp_mng_ctx)
    assert len(records) == 1
    assert records[0].trigger.name == "with-non-json"


def test_list_local_schedule_creation_records_skips_unreadable_files(
    temp_mng_ctx: MngContext,
) -> None:
    """list_local_schedule_creation_records should skip files that cannot be read."""
    trigger = _make_test_trigger("readable-trigger")
    deploy_local_schedule(
        trigger,
        temp_mng_ctx,
        crontab_reader=lambda: "",
        crontab_writer=lambda _: None,
        git_hash_resolver=lambda: "hash",
    )

    records_dir = temp_mng_ctx.config.default_host_dir.expanduser() / "schedule" / "records"
    unreadable = records_dir / "unreadable.json"
    unreadable.write_text("will be unreadable")
    unreadable.chmod(0o000)

    records = list_local_schedule_creation_records(temp_mng_ctx)
    assert len(records) == 1
    assert records[0].trigger.name == "readable-trigger"

    unreadable.chmod(0o644)
