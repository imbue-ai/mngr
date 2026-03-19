import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from io import StringIO
from typing import Any

from imbue.slack_exporter.channels import fetch_raw_channel_list
from imbue.slack_exporter.data_types import SlackApiCaller
from imbue.slack_exporter.latchkey import call_slack_api


def _format_timestamp(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


def _get_channel_updated_timestamp(channel: dict[str, Any]) -> float:
    """Extract the best available activity timestamp from a channel object.

    The Slack API returns "updated" in milliseconds and "created" in seconds.
    """
    updated_ms = channel.get("updated")
    if updated_ms is not None:
        return float(updated_ms) / 1000

    created_seconds = channel.get("created", 0)
    return float(created_seconds)


def fetch_and_sort_channels(
    api_caller: SlackApiCaller,
    members_only: bool,
) -> list[dict[str, Any]]:
    """Fetch channels via conversations.list and return sorted by most recent activity."""
    raw_channels = fetch_raw_channel_list(api_caller=api_caller, members_only=members_only)

    # Sort by the "updated" field (most recent first). This tracks the last time the
    # channel was modified (settings, topic, messages, etc.) and is the best activity
    # proxy available from conversations.list without per-channel API calls.
    return sorted(raw_channels, key=_get_channel_updated_timestamp, reverse=True)


def format_channel_table(channels: Sequence[dict[str, Any]]) -> str:
    """Format channels as a table string."""
    buf = StringIO()
    if not channels:
        buf.write("No channels found.\n")
        return buf.getvalue()

    buf.write(f"{'#':<4} {'CHANNEL':<30} {'LAST UPDATED':<18}\n")
    buf.write("-" * 52 + "\n")

    for idx, channel in enumerate(channels):
        name = channel.get("name", "unknown")
        updated_ts = _get_channel_updated_timestamp(channel)
        updated_str = _format_timestamp(updated_ts) if updated_ts > 0 else "unknown"
        buf.write(f"{idx + 1:<4} {name:<30} {updated_str:<18}\n")

    return buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List Slack channels sorted by most recent activity",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_channels",
        help="Include channels you're not a member of (default: only member channels)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    sorted_channels = fetch_and_sort_channels(
        api_caller=call_slack_api,
        members_only=not args.all_channels,
    )
    sys.stdout.write(format_channel_table(sorted_channels))


if __name__ == "__main__":
    main()
