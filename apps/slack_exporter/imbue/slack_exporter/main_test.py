from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest

from imbue.slack_exporter.data_types import ChannelConfig
from imbue.slack_exporter.data_types import ExporterSettings
from imbue.slack_exporter.errors import ChannelNotFoundError
from imbue.slack_exporter.errors import LatchkeyInvocationError
from imbue.slack_exporter.errors import SlackApiError
from imbue.slack_exporter.main import _build_arg_parser
from imbue.slack_exporter.main import _parse_channel_spec
from imbue.slack_exporter.main import build_settings_from_args
from imbue.slack_exporter.main import run_export_or_exit
from imbue.slack_exporter.primitives import SlackChannelName


def test_parse_channel_spec_simple_name() -> None:
    config = _parse_channel_spec("general")
    assert config.name == SlackChannelName("general")
    assert config.oldest is None


def test_parse_channel_spec_name_with_hash() -> None:
    config = _parse_channel_spec("#general")
    assert config.name == SlackChannelName("general")


def test_parse_channel_spec_name_with_date() -> None:
    config = _parse_channel_spec("general:2024-06-15")
    assert config.name == SlackChannelName("general")
    assert config.oldest == datetime(2024, 6, 15, tzinfo=timezone.utc)


def test_parse_channel_spec_name_with_datetime() -> None:
    config = _parse_channel_spec("random:2024-06-15T10:30:00")
    assert config.name == SlackChannelName("random")
    assert config.oldest == datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)


def test_parse_channel_spec_timezone_aware_input_converts_to_utc() -> None:
    config = _parse_channel_spec("general:2024-01-01T00:00:00+05:00")
    assert config.oldest == datetime(2023, 12, 31, 19, 0, 0, tzinfo=timezone.utc)


def test_build_settings_splits_space_separated_channels() -> None:
    # A single --channels value containing spaces expands into multiple channel configs.
    args = _build_arg_parser().parse_args(["--channels", "general random", "engineering"])
    settings = build_settings_from_args(args, {})
    assert settings.channels is not None
    assert tuple(c.name for c in settings.channels) == (
        SlackChannelName("general"),
        SlackChannelName("random"),
        SlackChannelName("engineering"),
    )


def test_build_settings_channels_none_when_unspecified() -> None:
    args = _build_arg_parser().parse_args([])
    settings = build_settings_from_args(args, {})
    assert settings.channels is None


def test_build_settings_refresh_window_zero_disables_window() -> None:
    # 0 means "disabled" (None), not a zero-day window.
    args = _build_arg_parser().parse_args(["--refresh-window-days", "0"])
    settings = build_settings_from_args(args, {})
    assert settings.refresh_window_days is None


def test_build_settings_refresh_window_positive_passes_through() -> None:
    args = _build_arg_parser().parse_args(["--refresh-window-days", "7"])
    settings = build_settings_from_args(args, {})
    assert settings.refresh_window_days == 7


def test_build_settings_cache_ttl_defaults_when_env_absent() -> None:
    args = _build_arg_parser().parse_args([])
    settings = build_settings_from_args(args, {})
    assert settings.cache_ttl_seconds == 600


def test_build_settings_cache_ttl_read_from_env() -> None:
    args = _build_arg_parser().parse_args([])
    settings = build_settings_from_args(args, {"SLACK_EXPORTER_CACHE_TTL_SECONDS": "42"})
    assert settings.cache_ttl_seconds == 42


@pytest.mark.parametrize(
    "error",
    [
        ChannelNotFoundError("general"),
        LatchkeyInvocationError("slack auth.test", 1, "boom"),
        SlackApiError("auth.test", "invalid_auth"),
    ],
)
def test_run_export_or_exit_maps_known_errors_to_exit_code_1(temp_output_dir: Path, error: Exception) -> None:
    def raising_caller(method: str, query_params: dict[str, str] | None = None) -> dict[str, Any]:
        raise error

    settings = ExporterSettings(
        channels=(ChannelConfig(name=SlackChannelName("general")),),
        default_oldest=datetime(2024, 1, 1, tzinfo=timezone.utc),
        output_dir=temp_output_dir,
        cache_ttl_seconds=0,
    )
    with pytest.raises(SystemExit) as exc_info:
        run_export_or_exit(settings, raising_caller)
    assert exc_info.value.code == 1
