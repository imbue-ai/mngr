from datetime import datetime
from datetime import timezone

from imbue.slack_exporter.main import _parse_channel_spec
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
