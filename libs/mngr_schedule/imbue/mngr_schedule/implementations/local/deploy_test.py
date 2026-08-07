"""Unit tests for local deploy.py."""

from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_schedule.data_types import ScheduleCreationRecord
from imbue.mngr_schedule.data_types import ScheduleTriggerDefinition
from imbue.mngr_schedule.data_types import ScheduledMngrCommand
from imbue.mngr_schedule.implementations.local.deploy import _get_records_dir
from imbue.mngr_schedule.implementations.local.deploy import _save_creation_record
from imbue.mngr_schedule.implementations.local.deploy import _stage_env_file
from imbue.mngr_schedule.implementations.local.deploy import build_wrapper_script
from imbue.mngr_schedule.implementations.local.deploy import deploy_local_schedule
from imbue.mngr_schedule.implementations.local.deploy import get_local_schedule_creation_record
from imbue.mngr_schedule.implementations.local.deploy import get_local_trigger_run_script
from imbue.mngr_schedule.implementations.local.deploy import list_local_schedule_creation_records

TriggerFactory = Callable[..., ScheduleTriggerDefinition]


# build_wrapper_script tests


def test_build_wrapper_script_without_env_file_renders_full_script() -> None:
    trigger = ScheduleTriggerDefinition(
        name="test",
        command=ScheduledMngrCommand.CREATE,
        args="--type claude --message 'do work'",
        schedule_cron="0 2 * * *",
        provider="local",
    )
    script = build_wrapper_script(
        trigger=trigger,
        working_directory="/home/user/project",
        path_value="/usr/local/bin:/usr/bin",
        env_file_path=None,
    )
    assert script == snapshot(
        """\
#!/usr/bin/env bash
set -euo pipefail

export PATH=/usr/local/bin:/usr/bin

cd /home/user/project

exec uv run mngr create --type claude --message 'do work'
"""
    )


def test_build_wrapper_script_with_env_file_sources_it_under_guard() -> None:
    trigger = ScheduleTriggerDefinition(
        name="test",
        command=ScheduledMngrCommand.CREATE,
        args="--type claude --message 'do work'",
        schedule_cron="0 2 * * *",
        provider="local",
    )
    script = build_wrapper_script(
        trigger=trigger,
        working_directory="/home/user/project",
        path_value="/usr/local/bin:/usr/bin",
        env_file_path=Path("/home/user/.mngr/schedule/test/.env"),
    )
    assert script == snapshot(
        """\
#!/usr/bin/env bash
set -euo pipefail

export PATH=/usr/local/bin:/usr/bin

if [ -f /home/user/.mngr/schedule/test/.env ]; then
    set -a
    source /home/user/.mngr/schedule/test/.env
    set +a
fi

cd /home/user/project

exec uv run mngr create --type claude --message 'do work'
"""
    )


def test_build_wrapper_script_empty_args_omits_trailing_args() -> None:
    trigger = ScheduleTriggerDefinition(
        name="test",
        command=ScheduledMngrCommand.EXEC,
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
    assert script == snapshot(
        """\
#!/usr/bin/env bash
set -euo pipefail

export PATH=/usr/bin

cd /tmp

exec uv run mngr exec
"""
    )


# _stage_env_file tests


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


# deploy_local_schedule tests (with injected crontab/git stubs)


def test_deploy_local_schedule_creates_files_and_record(
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    make_test_trigger: TriggerFactory,
) -> None:
    """Test the full deploy flow with injected crontab and git hash stubs."""
    captured_crontab: list[str] = []

    trigger = make_test_trigger()
    deploy_local_schedule(
        trigger,
        temp_mngr_ctx,
        sys_argv=["mngr", "schedule", "add"],
        crontab_reader=lambda: "",
        crontab_writer=captured_crontab.append,
        git_hash_resolver=lambda: "fakehash123",
    )

    # Verify crontab was written with the trigger
    assert len(captured_crontab) == 1
    assert "schedule:test-trigger" in captured_crontab[0]
    assert "0 2 * * *" in captured_crontab[0]

    # Verify wrapper script was created
    wrapper_script = tmp_path / ".mngr" / "schedule" / "triggers" / "test-trigger" / "run.sh"
    assert wrapper_script.exists()
    assert wrapper_script.stat().st_mode & 0o100  # executable

    # Verify creation record was saved
    records = list_local_schedule_creation_records(temp_mngr_ctx)
    assert len(records) == 1
    assert records[0].trigger.name == "test-trigger"
    assert records[0].mngr_git_hash == "fakehash123"


def test_deploy_local_schedule_with_env_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    temp_mngr_ctx: MngrContext,
    make_test_trigger: TriggerFactory,
) -> None:
    """Test that pass-env vars are included in the wrapper script."""
    monkeypatch.setenv("MY_API_KEY", "sk-test-123")

    trigger = make_test_trigger()
    deploy_local_schedule(
        trigger,
        temp_mngr_ctx,
        pass_env=["MY_API_KEY"],
        crontab_reader=lambda: "",
        crontab_writer=lambda content: None,
        git_hash_resolver=lambda: "fakehash",
    )

    # Verify env file was created
    env_file = tmp_path / ".mngr" / "schedule" / "triggers" / "test-trigger" / ".env"
    assert env_file.exists()
    assert "MY_API_KEY=sk-test-123" in env_file.read_text()

    # Verify wrapper script sources the env file
    wrapper = tmp_path / ".mngr" / "schedule" / "triggers" / "test-trigger" / "run.sh"
    assert "source" in wrapper.read_text()


def test_deploy_local_schedule_update_replaces_crontab_entry(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that deploying the same trigger name replaces the crontab entry.

    The crontab is modeled as a persistent store with last-write-wins
    semantics: the reader always returns the latest written content and the
    writer overwrites it. The assertions check the final crontab content,
    independent of how many times the reader is invoked.
    """
    current_crontab: list[str] = [""]

    def crontab_reader() -> str:
        return current_crontab[0]

    def crontab_writer(content: str) -> None:
        current_crontab[0] = content

    # Deploy first time
    trigger1 = ScheduleTriggerDefinition(
        name="my-trigger",
        command=ScheduledMngrCommand.CREATE,
        args="--message first",
        schedule_cron="0 1 * * *",
        provider="local",
    )
    deploy_local_schedule(
        trigger1,
        temp_mngr_ctx,
        crontab_reader=crontab_reader,
        crontab_writer=crontab_writer,
        git_hash_resolver=lambda: "fakehash",
    )

    # Deploy second time with different schedule
    trigger2 = ScheduleTriggerDefinition(
        name="my-trigger",
        command=ScheduledMngrCommand.CREATE,
        args="--message second",
        schedule_cron="0 3 * * *",
        provider="local",
    )
    deploy_local_schedule(
        trigger2,
        temp_mngr_ctx,
        crontab_reader=crontab_reader,
        crontab_writer=crontab_writer,
        git_hash_resolver=lambda: "fakehash",
    )

    # The old entry should be replaced by the new one (last-write-wins).
    final_crontab = current_crontab[0]
    assert final_crontab.count("schedule:my-trigger") == 1
    assert "0 3 * * *" in final_crontab
    assert "0 1 * * *" not in final_crontab


# list_local_schedule_creation_records tests


def test_list_local_schedule_creation_records_empty(
    temp_mngr_ctx: MngrContext,
) -> None:
    records = list_local_schedule_creation_records(temp_mngr_ctx)
    assert records == []


def test_list_local_schedule_creation_records_round_trip(
    temp_mngr_ctx: MngrContext,
    make_test_trigger: TriggerFactory,
) -> None:
    """Test that saved records can be read back."""
    trigger = make_test_trigger("my-schedule")
    record = ScheduleCreationRecord(
        trigger=trigger,
        full_commandline="mngr schedule add ...",
        hostname="testhost",
        working_directory="/tmp/test",
        mngr_git_hash="abc123",
        created_at=datetime(2025, 6, 15, 10, 0, 0, tzinfo=timezone.utc),
    )
    _save_creation_record(record, temp_mngr_ctx)

    records = list_local_schedule_creation_records(temp_mngr_ctx)
    assert len(records) == 1
    assert records[0].trigger.name == "my-schedule"
    assert records[0].hostname == "testhost"
    assert records[0].working_directory == "/tmp/test"


def test_list_local_schedule_creation_records_multiple(
    temp_mngr_ctx: MngrContext,
    make_test_trigger: TriggerFactory,
) -> None:
    """Test listing multiple records."""
    for name in ["alpha", "beta", "gamma"]:
        trigger = make_test_trigger(name)
        record = ScheduleCreationRecord(
            trigger=trigger,
            full_commandline=f"mngr schedule add --name {name}",
            hostname="testhost",
            working_directory="/tmp/test",
            mngr_git_hash="abc123",
            created_at=datetime(2025, 6, 15, 10, 0, 0, tzinfo=timezone.utc),
        )
        _save_creation_record(record, temp_mngr_ctx)

    records = list_local_schedule_creation_records(temp_mngr_ctx)
    assert len(records) == 3
    names = [r.trigger.name for r in records]
    assert "alpha" in names
    assert "beta" in names
    assert "gamma" in names


def test_list_local_schedule_creation_records_skips_invalid_json(
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that invalid JSON files are skipped with a warning."""
    records_dir = tmp_path / ".mngr" / "schedule" / "records"
    records_dir.mkdir(parents=True)
    (records_dir / "bad.json").write_text("not valid json")

    records = list_local_schedule_creation_records(temp_mngr_ctx)
    assert records == []


# deploy then list round-trip tests


def test_deploy_then_list_round_trip_preserves_all_fields(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that deploying a schedule and then listing it preserves all record fields."""
    trigger = ScheduleTriggerDefinition(
        name="integration-test",
        command=ScheduledMngrCommand.CREATE,
        args="--type claude --message 'hello world'",
        schedule_cron="30 3 * * 1-5",
        provider="local",
        is_enabled=True,
    )

    deploy_local_schedule(
        trigger,
        temp_mngr_ctx,
        sys_argv=["uv", "run", "mngr", "schedule", "add", "--provider", "local"],
        crontab_reader=lambda: "",
        crontab_writer=lambda _: None,
        git_hash_resolver=lambda: "mngr-hash-789",
    )

    records = list_local_schedule_creation_records(temp_mngr_ctx)
    assert len(records) == 1

    record = records[0]
    assert record.trigger.name == "integration-test"
    assert record.trigger.command == ScheduledMngrCommand.CREATE
    assert record.trigger.args == "--type claude --message 'hello world'"
    assert record.trigger.schedule_cron == "30 3 * * 1-5"
    assert record.trigger.provider == "local"
    assert record.trigger.is_enabled is True
    assert record.trigger.git_image_hash == ""
    assert record.mngr_git_hash == "mngr-hash-789"
    assert record.hostname != ""
    assert record.working_directory != ""
    assert "uv run mngr schedule add" in record.full_commandline


# list_local_schedule_creation_records edge cases


def test_list_local_schedule_creation_records_skips_non_json_files(
    temp_mngr_ctx: MngrContext,
    make_test_trigger: TriggerFactory,
) -> None:
    """list_local_schedule_creation_records should skip non-JSON files."""
    trigger = make_test_trigger("with-non-json")
    deploy_local_schedule(
        trigger,
        temp_mngr_ctx,
        crontab_reader=lambda: "",
        crontab_writer=lambda _: None,
        git_hash_resolver=lambda: "hash",
    )

    # Create a non-json file in the records directory
    records_dir = temp_mngr_ctx.config.default_host_dir.expanduser() / "schedule" / "records"
    (records_dir / "README.txt").write_text("not a record")

    records = list_local_schedule_creation_records(temp_mngr_ctx)
    assert len(records) == 1
    assert records[0].trigger.name == "with-non-json"


def test_list_local_schedule_creation_records_skips_unreadable_files(
    temp_mngr_ctx: MngrContext,
    make_test_trigger: TriggerFactory,
) -> None:
    """list_local_schedule_creation_records should skip records that raise OSError on read.

    A directory whose name ends in ``.json`` passes the name filter but makes
    ``read_bytes()`` raise ``IsADirectoryError`` (an ``OSError``) regardless of
    the running uid -- so this exercises the OSError skip branch even when the
    test runs as root, where permission bits would be bypassed. A sibling valid
    record must still be returned, so the test fails loudly if that branch ever
    stops skipping.
    """
    trigger = make_test_trigger("readable-trigger")
    deploy_local_schedule(
        trigger,
        temp_mngr_ctx,
        crontab_reader=lambda: "",
        crontab_writer=lambda _: None,
        git_hash_resolver=lambda: "hash",
    )

    records_dir = temp_mngr_ctx.config.default_host_dir.expanduser() / "schedule" / "records"
    # A directory named like a record: read_bytes() raises IsADirectoryError.
    (records_dir / "unreadable.json").mkdir()

    records = list_local_schedule_creation_records(temp_mngr_ctx)
    assert len(records) == 1
    assert records[0].trigger.name == "readable-trigger"


# get_local_schedule_creation_record tests


def test_get_local_schedule_creation_record_found(
    temp_mngr_ctx: MngrContext,
    make_test_trigger: TriggerFactory,
) -> None:
    """Looking up a deployed trigger by name should return its record."""
    trigger = make_test_trigger("my-trigger")
    deploy_local_schedule(
        trigger,
        temp_mngr_ctx,
        crontab_reader=lambda: "",
        crontab_writer=lambda _: None,
        git_hash_resolver=lambda: "fakehash",
    )

    record = get_local_schedule_creation_record(temp_mngr_ctx, "my-trigger")
    assert record is not None
    assert record.trigger.name == "my-trigger"
    assert record.trigger.command == ScheduledMngrCommand.CREATE


def test_get_local_schedule_creation_record_not_found(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Looking up a nonexistent trigger should return None."""
    record = get_local_schedule_creation_record(temp_mngr_ctx, "nonexistent")
    assert record is None


def test_get_local_schedule_creation_record_invalid_json(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Looking up a trigger with invalid JSON should return None."""
    # Resolve the records dir via the same helper the production code uses
    # so this test does not silently decouple from a layout change.
    records_dir = _get_records_dir(temp_mngr_ctx)
    records_dir.mkdir(parents=True, exist_ok=True)
    (records_dir / "bad-trigger.json").write_text("not valid json")

    record = get_local_schedule_creation_record(temp_mngr_ctx, "bad-trigger")
    assert record is None


# get_local_trigger_run_script tests


def test_get_local_trigger_run_script_returns_correct_path(
    temp_mngr_ctx: MngrContext,
) -> None:
    """Should return the path to run.sh inside the trigger directory."""
    path = get_local_trigger_run_script(temp_mngr_ctx, "my-trigger")
    assert path.name == "run.sh"
    assert "my-trigger" in str(path)


def test_get_local_trigger_run_script_exists_after_deploy(
    temp_mngr_ctx: MngrContext,
    make_test_trigger: TriggerFactory,
) -> None:
    """After deploying a trigger, its run.sh should exist."""
    trigger = make_test_trigger("deployed-trigger")
    deploy_local_schedule(
        trigger,
        temp_mngr_ctx,
        crontab_reader=lambda: "",
        crontab_writer=lambda _: None,
        git_hash_resolver=lambda: "fakehash",
    )

    path = get_local_trigger_run_script(temp_mngr_ctx, "deployed-trigger")
    assert path.is_file()
