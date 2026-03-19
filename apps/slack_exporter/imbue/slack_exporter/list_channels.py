import argparse
import logging
import sys
from datetime import datetime
from datetime import timezone
from typing import Any

from imbue.slack_exporter.latchkey import call_slack_api
from imbue.slack_exporter.latchkey import fetch_paginated


def _format_timestamp(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


def _fetch_and_print_channels(members_only: bool) -> None:
    """Fetch channels via conversations.list and write to stdout sorted by most recent activity."""
    raw_channels = fetch_paginated(
        api_caller=call_slack_api,
        method="conversations.list",
        base_params={
            "exclude_archived": "true",
            "limit": "200",
            "types": "public_channel,private_channel",
        },
        response_key="channels",
    )

    if members_only:
        raw_channels = [ch for ch in raw_channels if ch.get("is_member", False)]

    # Sort by the "updated" field (most recent first). This tracks the last time the
    # channel was modified (settings, topic, messages, etc.) and is the best activity
    # proxy available from conversations.list without per-channel API calls.
    sorted_channels = sorted(raw_channels, key=_get_channel_updated_timestamp, reverse=True)

    _write_channel_table(sorted_channels)


def _get_channel_updated_timestamp(channel: dict[str, Any]) -> float:
    return float(channel.get("updated", channel.get("created", 0)) / 1000)


def _write_channel_table(channels: list[dict[str, Any]]) -> None:
    """Write channels as a formatted table to stdout."""
    out = sys.stdout
    if not channels:
        out.write("No channels found.\n")
        return

    out.write(f"{'#':<4} {'CHANNEL':<30} {'LAST UPDATED':<18}\n")
    out.write("-" * 52 + "\n")

    for idx, channel in enumerate(channels):
        name = channel.get("name", "unknown")
        updated_ms = channel.get("updated", channel.get("created", 0))
        updated_ts = float(updated_ms) / 1000
        updated_str = _format_timestamp(updated_ts) if updated_ts > 0 else "unknown"
        out.write(f"{idx + 1:<4} {name:<30} {updated_str:<18}\n")


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

    _fetch_and_print_channels(members_only=not args.all_channels)


if __name__ == "__main__":
    main()
