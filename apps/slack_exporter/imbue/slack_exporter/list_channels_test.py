from imbue.slack_exporter.list_channels import _get_channel_updated_timestamp
from imbue.slack_exporter.list_channels import fetch_and_sort_channels
from imbue.slack_exporter.list_channels import format_channel_table
from imbue.slack_exporter.testing import make_fake_api_caller
from imbue.slack_exporter.testing import make_slack_response


def test_get_channel_updated_timestamp_uses_updated_field_in_milliseconds() -> None:
    channel = {"updated": 1700000000000, "created": 1600000000}
    result = _get_channel_updated_timestamp(channel)
    assert result == 1700000000.0


def test_get_channel_updated_timestamp_falls_back_to_created_in_seconds() -> None:
    channel = {"created": 1600000000}
    result = _get_channel_updated_timestamp(channel)
    assert result == 1600000000.0


def test_get_channel_updated_timestamp_returns_zero_when_missing() -> None:
    result = _get_channel_updated_timestamp({})
    assert result == 0.0


def test_fetch_and_sort_channels_sorts_by_updated_descending() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.list": [
                make_slack_response(
                    "channels",
                    [
                        {"id": "C1", "name": "old", "is_member": True, "updated": 1000000000000},
                        {"id": "C2", "name": "new", "is_member": True, "updated": 1700000000000},
                        {"id": "C3", "name": "mid", "is_member": True, "updated": 1400000000000},
                    ],
                ),
            ],
        }
    )

    result = fetch_and_sort_channels(api_caller=api_caller, members_only=True)

    assert [ch["name"] for ch in result] == ["new", "mid", "old"]


def test_fetch_and_sort_channels_filters_non_member_channels() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.list": [
                make_slack_response(
                    "channels",
                    [
                        {"id": "C1", "name": "member", "is_member": True, "updated": 1700000000000},
                        {"id": "C2", "name": "not-member", "is_member": False, "updated": 1700000000000},
                    ],
                ),
            ],
        }
    )

    result = fetch_and_sort_channels(api_caller=api_caller, members_only=True)

    assert len(result) == 1
    assert result[0]["name"] == "member"


def test_fetch_and_sort_channels_includes_all_when_members_only_false() -> None:
    api_caller = make_fake_api_caller(
        {
            "conversations.list": [
                make_slack_response(
                    "channels",
                    [
                        {"id": "C1", "name": "member", "is_member": True, "updated": 1700000000000},
                        {"id": "C2", "name": "not-member", "is_member": False, "updated": 1700000000000},
                    ],
                ),
            ],
        }
    )

    result = fetch_and_sort_channels(api_caller=api_caller, members_only=False)

    assert len(result) == 2


def test_format_channel_table_empty_list() -> None:
    result = format_channel_table([])
    assert result == "No channels found.\n"


def test_format_channel_table_formats_channels() -> None:
    channels = [
        {"name": "general", "updated": 1700000000000},
        {"name": "random", "updated": 1600000000000},
    ]

    result = format_channel_table(channels)

    assert "general" in result
    assert "random" in result
    assert "2023-11-14" in result
    # Verify header and separator are present
    assert "CHANNEL" in result
    assert "LAST UPDATED" in result
    assert "---" in result


def test_format_channel_table_falls_back_to_created_for_display() -> None:
    channels = [{"name": "old-channel", "created": 1600000000}]

    result = format_channel_table(channels)

    assert "old-channel" in result
    # created=1600000000 -> 2020-09-13
    assert "2020-09-13" in result
