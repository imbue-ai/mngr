"""Unit tests for schedule CLI output helpers."""

import json
from typing import Callable

import pytest

from imbue.imbue_common.errors import SwitchError
from imbue.mngr_schedule.cli.list import _emit_schedule_list_human
from imbue.mngr_schedule.cli.list import _emit_schedule_list_json
from imbue.mngr_schedule.cli.list import _emit_schedule_list_jsonl
from imbue.mngr_schedule.cli.list import _get_schedule_field_value
from imbue.mngr_schedule.data_types import ScheduleCreationRecord
from imbue.mngr_schedule.data_types import ScheduleTriggerDefinition

# The full git image hash used by the modal record in these tests. The
# git_hash display field truncates this to its first 12 characters.
_FULL_GIT_IMAGE_HASH = "abc123def456789012345678901234567890abcd"


def _build_modal_record(
    make_test_trigger: Callable[..., ScheduleTriggerDefinition],
    make_schedule_record: Callable[..., ScheduleCreationRecord],
    *,
    is_enabled: bool = True,
) -> ScheduleCreationRecord:
    trigger = make_test_trigger(
        "nightly-build",
        provider="modal",
        is_enabled=is_enabled,
        git_image_hash=_FULL_GIT_IMAGE_HASH,
    )
    return make_schedule_record(trigger=trigger)


def test_get_schedule_field_value_command(
    make_test_trigger: Callable[..., ScheduleTriggerDefinition],
    make_schedule_record: Callable[..., ScheduleCreationRecord],
) -> None:
    record = _build_modal_record(make_test_trigger, make_schedule_record)
    assert _get_schedule_field_value(record, "command") == "create"


def test_get_schedule_field_value_enabled(
    make_test_trigger: Callable[..., ScheduleTriggerDefinition],
    make_schedule_record: Callable[..., ScheduleCreationRecord],
) -> None:
    record = _build_modal_record(make_test_trigger, make_schedule_record)
    assert _get_schedule_field_value(record, "enabled") == "yes"


def test_get_schedule_field_value_enabled_when_disabled(
    make_test_trigger: Callable[..., ScheduleTriggerDefinition],
    make_schedule_record: Callable[..., ScheduleCreationRecord],
) -> None:
    record = _build_modal_record(make_test_trigger, make_schedule_record, is_enabled=False)
    assert _get_schedule_field_value(record, "enabled") == "no"


def test_get_schedule_field_value_git_hash_truncates_to_12_chars(
    make_test_trigger: Callable[..., ScheduleTriggerDefinition],
    make_schedule_record: Callable[..., ScheduleCreationRecord],
) -> None:
    record = _build_modal_record(make_test_trigger, make_schedule_record)
    result = _get_schedule_field_value(record, "git_hash")
    assert result == "abc123def456"
    assert len(result) == 12


def test_get_schedule_field_value_git_hash_returns_empty_when_not_set(
    make_test_trigger: Callable[..., ScheduleTriggerDefinition],
    make_schedule_record: Callable[..., ScheduleCreationRecord],
) -> None:
    record = make_schedule_record(trigger=make_test_trigger("nightly-build", provider="local"), is_modal=False)
    result = _get_schedule_field_value(record, "git_hash")
    assert result == ""


def test_get_schedule_field_value_created_at(
    make_test_trigger: Callable[..., ScheduleTriggerDefinition],
    make_schedule_record: Callable[..., ScheduleCreationRecord],
) -> None:
    record = _build_modal_record(make_test_trigger, make_schedule_record)
    result = _get_schedule_field_value(record, "created_at")
    assert result == "2025-06-15 14:30"


def test_get_schedule_field_value_unknown_field_raises_switch_error(
    make_test_trigger: Callable[..., ScheduleTriggerDefinition],
    make_schedule_record: Callable[..., ScheduleCreationRecord],
) -> None:
    record = _build_modal_record(make_test_trigger, make_schedule_record)
    with pytest.raises(SwitchError, match="Unknown schedule display field"):
        _get_schedule_field_value(record, "nonexistent")


def test_emit_schedule_list_human_with_records(
    make_test_trigger: Callable[..., ScheduleTriggerDefinition],
    make_schedule_record: Callable[..., ScheduleCreationRecord],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Human output should emit a table with name, schedule, and transformed columns."""
    records = [_build_modal_record(make_test_trigger, make_schedule_record)]
    _emit_schedule_list_human(records)
    captured = capsys.readouterr()
    assert "nightly-build" in captured.out
    assert "0 2 * * *" in captured.out
    # The git hash column is truncated to 12 chars and created_at is strftime-formatted,
    # so confirm the formatter routes these through _get_schedule_field_value.
    assert "abc123def456" in captured.out
    assert "2025-06-15 14:30" in captured.out


def test_emit_schedule_list_human_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """Human output with no records should emit 'No schedules found'."""
    _emit_schedule_list_human([])
    captured = capsys.readouterr()
    assert "No schedules found" in captured.out


def test_emit_schedule_list_json_with_records(
    make_test_trigger: Callable[..., ScheduleTriggerDefinition],
    make_schedule_record: Callable[..., ScheduleCreationRecord],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSON output should serialize each record's full structure under 'schedules'."""
    records = [_build_modal_record(make_test_trigger, make_schedule_record)]
    _emit_schedule_list_json(records)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data["schedules"]) == 1
    assert data["schedules"][0]["trigger"]["name"] == "nightly-build"
    assert data["schedules"][0]["trigger"]["git_image_hash"] == _FULL_GIT_IMAGE_HASH


def test_emit_schedule_list_jsonl_with_records(
    make_test_trigger: Callable[..., ScheduleTriggerDefinition],
    make_schedule_record: Callable[..., ScheduleCreationRecord],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JSONL output should emit one JSON line per record."""
    record = _build_modal_record(make_test_trigger, make_schedule_record)
    _emit_schedule_list_jsonl([record, record])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.strip().splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["trigger"]["name"] == "nightly-build"
