from imbue.slack_exporter.list_channels import _get_channel_updated_timestamp


def test_get_channel_updated_timestamp_uses_updated_field() -> None:
    channel = {"updated": 1700000000000, "created": 1600000000}
    result = _get_channel_updated_timestamp(channel)
    assert result == 1700000000.0


def test_get_channel_updated_timestamp_falls_back_to_created() -> None:
    channel = {"created": 1600000000000}
    result = _get_channel_updated_timestamp(channel)
    assert result == 1600000000.0


def test_get_channel_updated_timestamp_returns_zero_when_missing() -> None:
    result = _get_channel_updated_timestamp({})
    assert result == 0.0
