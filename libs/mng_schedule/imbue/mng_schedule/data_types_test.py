"""Unit tests for mng_schedule data types."""

from datetime import datetime
from datetime import timezone

from imbue.mng_schedule.data_types import ModalScheduleCreationRecord
from imbue.mng_schedule.data_types import ScheduleCreationRecord
from imbue.mng_schedule.data_types import ScheduleTriggerDefinition
from imbue.mng_schedule.data_types import ScheduledMngCommand


def test_modal_schedule_creation_record_round_trips_through_json() -> None:
    """Test that ModalScheduleCreationRecord serializes and deserializes correctly."""
    trigger = ScheduleTriggerDefinition(
        name="nightly-create",
        command=ScheduledMngCommand.CREATE,
        args="--type claude --message 'fix bugs'",
        schedule_cron="0 2 * * *",
        provider="modal",
        is_enabled=True,
    )
    record = ModalScheduleCreationRecord(
        trigger=trigger,
        full_commandline="uv run mng schedule add --command create --schedule '0 2 * * *'",
        hostname="dev-machine",
        working_directory="/home/user/project",
        mng_git_hash="fedcba654321",
        created_at=datetime(2025, 6, 15, 10, 30, 0, tzinfo=timezone.utc),
        app_name="mng-schedule-nightly-create",
        environment="mng-user1",
    )

    json_data = record.model_dump_json()
    restored = ModalScheduleCreationRecord.model_validate_json(json_data)

    assert restored.trigger.name == "nightly-create"
    assert restored.trigger.command == ScheduledMngCommand.CREATE
    assert restored.trigger.args == "--type claude --message 'fix bugs'"
    assert restored.hostname == "dev-machine"
    assert restored.working_directory == "/home/user/project"
    assert restored.mng_git_hash == "fedcba654321"
    assert restored.app_name == "mng-schedule-nightly-create"
    assert restored.environment == "mng-user1"


def test_base_schedule_creation_record_round_trips_through_json() -> None:
    """Test that the base ScheduleCreationRecord (for local provider) works."""
    trigger = ScheduleTriggerDefinition(
        name="local-trigger",
        command=ScheduledMngCommand.CREATE,
        args="--type claude",
        schedule_cron="0 2 * * *",
        provider="local",
        is_enabled=True,
    )
    record = ScheduleCreationRecord(
        trigger=trigger,
        full_commandline="mng schedule add --command create --provider local",
        hostname="dev-machine",
        working_directory="/home/user/project",
        mng_git_hash="fedcba654321",
        created_at=datetime(2025, 6, 15, 10, 30, 0, tzinfo=timezone.utc),
    )

    json_data = record.model_dump_json()
    restored = ScheduleCreationRecord.model_validate_json(json_data)

    assert restored.trigger.name == "local-trigger"
    assert restored.trigger.provider == "local"
    assert restored.hostname == "dev-machine"


def test_modal_record_deserializes_old_field_names() -> None:
    """Test that ModalScheduleCreationRecord can deserialize JSON with old field names."""
    old_json = (
        '{"trigger":{"name":"old","command":"CREATE","args":"","schedule_cron":"0 2 * * *",'
        '"provider":"modal","is_enabled":true,"git_image_hash":"abc123"},'
        '"full_commandline":"mng schedule add","hostname":"laptop","working_directory":"/tmp",'
        '"mng_git_hash":"abc123","created_at":"2025-06-15T10:00:00Z",'
        '"modal_app_name":"mng-schedule-old","modal_environment":"mng-user1"}'
    )
    record = ModalScheduleCreationRecord.model_validate_json(old_json)
    assert record.app_name == "mng-schedule-old"
    assert record.environment == "mng-user1"


def test_schedule_creation_record_includes_all_trigger_fields() -> None:
    """Test that the nested trigger definition is fully preserved."""
    trigger = ScheduleTriggerDefinition(
        name="test-trigger",
        command=ScheduledMngCommand.EXEC,
        args="--exec 'echo hello'",
        schedule_cron="*/5 * * * *",
        provider="modal",
        is_enabled=False,
    )
    record = ModalScheduleCreationRecord(
        trigger=trigger,
        full_commandline="mng schedule add --name test-trigger",
        hostname="laptop",
        working_directory="/tmp",
        mng_git_hash="1234abcd",
        created_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        app_name="mng-schedule-test-trigger",
        environment="mng-testuser",
    )

    assert record.trigger.is_enabled is False
    assert record.trigger.command == ScheduledMngCommand.EXEC
    assert record.trigger.schedule_cron == "*/5 * * * *"
