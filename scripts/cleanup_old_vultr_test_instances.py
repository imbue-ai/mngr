#!/usr/bin/env python3
"""Script to clean up old Vultr test VPS instances.

This script is designed to be run periodically (e.g. in CI on a schedule) to
destroy Vultr instances created by the mngr_vultr release tests that were left
behind when a test session was killed before its in-process cleanup
(``pytest_sessionfinish`` in ``libs/mngr_vultr/imbue/mngr_vultr/conftest.py``)
could run. It selects instances by the ``mngr-vultr-test-created=<timestamp>``
tag that conftest attaches at create time and destroys those older than
``--max-age-hours``. Production VPSes never carry that tag, so they are never
touched.

Skips silently (exit 0) when ``VULTR_API_KEY`` is unset, so it is safe to wire
into CI before the secret is configured.

Usage:
    uv run python scripts/cleanup_old_vultr_test_instances.py [--max-age-hours HOURS]

Options:
    --max-age-hours  Maximum age in hours for test instances to keep (default: 1.0)
"""

import argparse
import os
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from pydantic import SecretStr

from imbue.imbue_common.logging import setup_logging
from imbue.mngr_vultr.cleanup import cleanup_old_vultr_test_instances
from imbue.mngr_vultr.client import VultrVpsClient

# os_id is required by the VultrVpsClient constructor but only used by
# create_instance, which the reaper never calls. Mirrors the hardcoded id in
# conftest.py and test_release_vultr.py.
_REAPER_OS_ID = 2136


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean up old Vultr test VPS instances",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=1.0,
        help="Maximum age in hours for test instances to keep (default: 1.0)",
    )
    args = parser.parse_args()

    setup_logging(level="INFO")

    api_key = os.environ.get("VULTR_API_KEY", "")
    if not api_key:
        print("VULTR_API_KEY not set; skipping Vultr test instance cleanup")
        return 0

    client = VultrVpsClient(api_key=SecretStr(api_key), os_id=_REAPER_OS_ID)
    cleaned_count = cleanup_old_vultr_test_instances(
        client,
        max_age=timedelta(hours=args.max_age_hours),
        now=datetime.now(timezone.utc),
    )

    if cleaned_count > 0:
        print(f"Cleaned up {cleaned_count} old Vultr test instance(s)")
    else:
        print("No old Vultr test instances found to clean up")

    return 0


if __name__ == "__main__":
    sys.exit(main())
