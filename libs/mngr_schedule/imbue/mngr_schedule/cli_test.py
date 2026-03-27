"""Unit tests for schedule CLI output helpers."""

from datetime import datetime
from datetime import timezone

import pytest

from imbue.imbue_common.errors import SwitchError
from imbue.mngr_schedule.cli.list import _emit_schedule_list_human
from imbue.mngr_schedule.cli.list import _emit_schedule_list_json
from imbue.mngr_schedule.cli.list import _emit_schedule_list_jsonl
from imbue.mngr_schedule.cli.list import _get_schedule_field_value
from imbue.mngr_schedule.data_types import ModalScheduleCreationRecord
from imbue.mngr_schedule.data_types import ScheduleCreationRecord
from imbue.mngr_schedule.data_types import ScheduleTriggerDefinition
from imbue.mngr_schedule.data_types import ScheduledMngrCommand


def _make_test_record(is_enabled: bool = True) -> ScheduleCreationRecord:
    trigger = ScheduleTriggerDefinition(
        name="nightly-build",
        command=ScheduledMngrCommand.CREATE,
        args="--type claude",
        schedule_cron="0 2 * * *",
        provider="modal",
        is_enabled=is_enabled,
        git_image_hash="abc123def456789012345678901234567890abcd",
    )
    return ModalScheduleCreationRecord(
        trigger=trigger,
        full_commandline="uv run mngr schedule add --command create",
        hostname="dev-laptop",
        working_directory="/home/user/project",
        mngr_git_hash="fedcba654321",
        created_at=datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
        app_name="mngr-schedule-nightly-build",
        environment="mngr-user1",
    )


def test_get_schedule_field_value_name() -> None:
    record = _make_test_record()
    assert _get_schedule_field_value(record, "name") == "nightly-build"


def test_get_schedule_field_value_command() -> None:
    record = _make_test_record()
    assert _get_schedule_field_value(record, "command") == "create"


def test_get_schedule_field_value_schedule() -> None:
    record = _make_test_record()
    assert _get_schedule_field_value(record, "schedule") == "0 2 * * *"


def test_get_schedule_field_value_enabled() -> None:
    record = _make_test_record()
    assert _get_schedule_field_value(record, "enabled") == "yes"


def test_get_schedule_field_value_enabled_when_disabled() -> None:
    record = _make_test_record(is_enabled=False)
    assert _get_schedule_field_value(record, "enabled") == "no"


def test_get_schedule_field_value_provider() -> None:
    record = _make_test_record()
    assert _get_schedule_field_value(record, "provider") == "modal"


def test_get_schedule_field_value_git_hash_truncates_to_12_chars() -> None:
    record = _make_test_record()
    result = _get_schedule_field_value(record, "git_hash")
    assert result == "abc123def456"
    assert len(result) == 12


def test_get_schedule_field_value_git_hash_returns_empty_when_not_set() -> None:
    trigger = ScheduleTriggerDefinition(
        name="nightly-build",
        command=ScheduledMngrCommand.CREATE,
        args="--type claude",
        schedule_cron="0 2 * * *",
        provider="local",
    )
    record = ScheduleCreationRecord(
        trigger=trigger,
        full_commandline="uv run mngr schedule add --command create",
        hostname="dev-laptop",
        working_directory="/home/user/project",
        mngr_git_hash="fedcba654321",
        created_at=datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc),
    )
    result = _get_schedule_field_value(record, "git_hash")
    assert result == ""


def test_get_schedule_field_value_created_at() -> None:
    record = _make_test_record()
    result = _get_schedule_field_value(record, "created_at")
    assert result == "2025-06-15 14:30"


def test_get_schedule_field_value_hostname() -> None:
    record = _make_test_record()
    assert _get_schedule_field_value(record, "hostname") == "dev-laptop"


def test_get_schedule_field_value_unknown_field_raises_switch_error() -> None:
    record = _make_test_record()
    with pytest.raises(SwitchError, match="Unknown schedule display field"):
        _get_schedule_field_value(record, "nonexistent")


# =============================================================================
# Tests for list output helpers
# =============================================================================


def test_emit_schedule_list_human_with_records(capsys: pytest.CaptureFixture[str]) -> None:
    """Human output should emit a table containing the schedule name."""
    records = [_make_test_record()]
    _emit_schedule_list_human(records)
    captured = capsys.readouterr()
    assert "nightly-build" in captured.out
    assert "0 2 * * *" in captured.out


def test_emit_schedule_list_human_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """Human output with no records should emit 'No schedules found'."""
    _emit_schedule_list_human([])
    captured = capsys.readouterr()
    assert "No schedules found" in captured.out


def test_emit_schedule_list_json_with_records(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON output should emit a JSON object with a 'schedules' key."""
    records = [_make_test_record()]
    _emit_schedule_list_json(records)
    captured = capsys.readouterr()
    assert '"schedules"' in captured.out


def test_emit_schedule_list_jsonl_with_records(capsys: pytest.CaptureFixture[str]) -> None:
    """JSONL output should emit one JSON line per record."""
    records = [_make_test_record(), _make_test_record()]
    _emit_schedule_list_jsonl(records)
    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line.strip()]
    assert len(lines) == 2
