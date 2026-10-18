from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds.desktop_client.app_record import APP_LAUNCH_ARGV_ENV_VAR
from imbue.minds.desktop_client.app_record import AppLaunchCommand
from imbue.minds.desktop_client.app_record import AppLaunchCommandError
from imbue.minds.desktop_client.app_record import app_record_path
from imbue.minds.desktop_client.app_record import build_app_record
from imbue.minds.desktop_client.app_record import parse_launch_command_from_environ
from imbue.minds.desktop_client.app_record import read_app_record
from imbue.minds.desktop_client.app_record import remove_app_record
from imbue.minds.desktop_client.app_record import write_app_record


def test_parse_launch_command_reads_a_json_argv_and_is_none_when_unset() -> None:
    parsed = parse_launch_command_from_environ({APP_LAUNCH_ARGV_ENV_VAR: '["open", "-g", "-a", "Imbue Studio"]'})
    assert parsed == AppLaunchCommand(argv=("open", "-g", "-a", "Imbue Studio"))
    assert parse_launch_command_from_environ({}) is None
    assert parse_launch_command_from_environ({APP_LAUNCH_ARGV_ENV_VAR: "  "}) is None


@pytest.mark.parametrize("raw", ["not json", '"a string"', "[]", '["ok", 3]', "{}"])
def test_parse_launch_command_rejects_anything_but_a_non_empty_string_array(raw: str) -> None:
    with pytest.raises(AppLaunchCommandError):
        parse_launch_command_from_environ({APP_LAUNCH_ARGV_ENV_VAR: raw})


def test_app_record_round_trips_and_is_removed_cleanly(tmp_path: Path) -> None:
    path = app_record_path(tmp_path / "state")
    record = build_app_record(
        tier="staging",
        root_name="minds-staging",
        version="0.8.6",
        socket_path=tmp_path / "api.sock",
        http_url="http://127.0.0.1:52817",
        launch=AppLaunchCommand(argv=("/opt/imbue-studio/imbue-studio", "--background")),
        started_at=datetime(2026, 10, 15, 17, 2, 11, tzinfo=timezone.utc),
    )

    write_app_record(path, record)
    loaded = read_app_record(path)

    assert loaded == record
    assert loaded.launch is not None
    assert loaded.launch.argv[-1] == "--background"
    remove_app_record(path)
    assert not path.exists()
    # Removing an absent record is not an error: a crashed run may already have none.
    remove_app_record(path)


def test_app_record_without_a_launch_command_round_trips(tmp_path: Path) -> None:
    path = app_record_path(tmp_path)
    record = build_app_record(
        tier="production",
        root_name="minds",
        version="unknown",
        socket_path=tmp_path / "api.sock",
        http_url="http://127.0.0.1:8420",
        launch=None,
        started_at=datetime.now(timezone.utc),
    )
    write_app_record(path, record)
    assert read_app_record(path).launch is None
