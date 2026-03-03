#!/usr/bin/env python3
"""Script to clean up old Modal test environments.

This script is designed to be run in CI to periodically clean up Modal test
environments that are older than a specified age. This helps prevent accumulation
of stale environments when test processes crash without proper cleanup.

Usage:
    uv run python scripts/cleanup_old_modal_test_environments.py [--max-age-hours HOURS]

Options:
    --max-age-hours  Maximum age in hours for environments to keep (default: 1.0)
"""

import argparse
import sys

from imbue.imbue_common.logging import setup_logging
from imbue.mng.testing.testing import cleanup_old_modal_test_environments


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean up old Modal test environments",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=1.0,
        help="Maximum age in hours for environments to keep (default: 1.0)",
    )
    args = parser.parse_args()

    setup_logging(level="INFO")

    cleaned_count = cleanup_old_modal_test_environments(max_age_hours=args.max_age_hours)

    if cleaned_count > 0:
        print(f"Cleaned up {cleaned_count} old Modal test environment(s)")
    else:
        print("No old Modal test environments found to clean up")

    return 0


if __name__ == "__main__":
    sys.exit(main())
