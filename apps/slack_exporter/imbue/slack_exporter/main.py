import argparse
import logging
import os
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.slack_exporter.data_types import ChannelConfig
from imbue.slack_exporter.data_types import ExporterSettings
from imbue.slack_exporter.errors import ChannelNotFoundError
from imbue.slack_exporter.errors import LatchkeyInvocationError
from imbue.slack_exporter.errors import SlackApiError
from imbue.slack_exporter.exporter import run_export
from imbue.slack_exporter.latchkey import call_slack_api
from imbue.slack_exporter.primitives import SlackChannelName


def _parse_iso_datetime_as_utc(value: str) -> datetime:
    """Parse an ISO datetime string, treating naive datetimes as UTC."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_channel_spec(spec: str) -> ChannelConfig:
    """Parse a channel spec like 'general' or 'general:2024-01-01'."""
    parts = spec.split(":", maxsplit=1)
    name = parts[0].lstrip("#")
    oldest: datetime | None = None
    if len(parts) == 2:
        oldest = _parse_iso_datetime_as_utc(parts[1])
    return ChannelConfig(name=SlackChannelName(name), oldest=oldest)


def main() -> None:
    """Entry point for the slack-exporter CLI."""
    parser = argparse.ArgumentParser(
        description="Export Slack channel messages, channels, and users to JSONL files using latchkey",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  slack-exporter
  slack-exporter --channels general random
  slack-exporter --channels "general:2024-01-01" "random"
  slack-exporter --since 2024-06-01 --output-dir my_export
        """,
    )

    parser.add_argument(
        "--channels",
        nargs="+",
        default=["general"],
        help="Channels to export (e.g. 'general' or 'general:2024-01-01' for per-channel oldest date)",
    )
    parser.add_argument(
        "--since",
        type=str,
        default="2024-01-01",
        help="Default oldest date for messages (ISO format, e.g. 2024-01-01). Default: 2024-01-01",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("slack_export"),
        help="Directory for output data (default: slack_export/)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_channels",
        help="Export all channels, not just those where you are a member",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force re-fetch of all cached data (channels, users, self identity, reactions)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    channel_configs = tuple(_parse_channel_spec(spec) for spec in args.channels)
    default_oldest = _parse_iso_datetime_as_utc(args.since)
    cache_ttl_seconds = int(os.environ.get("SLACK_EXPORTER_CACHE_TTL_SECONDS", "600"))

    settings = ExporterSettings(
        channels=channel_configs,
        default_oldest=default_oldest,
        output_dir=args.output_dir,
        members_only=not args.all_channels,
        refresh=args.refresh,
        cache_ttl_seconds=cache_ttl_seconds,
    )

    try:
        run_export(settings, api_caller=call_slack_api)
    except ChannelNotFoundError as e:
        logging.error("Channel not found: %s", e.channel_name)
        sys.exit(1)
    except LatchkeyInvocationError as e:
        logging.error("Latchkey error: %s", e)
        sys.exit(1)
    except SlackApiError as e:
        logging.error("Slack API error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
