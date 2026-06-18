#!/usr/bin/env python3
"""Script to clean up old AWS test EC2 instances.

This script is run by a CI job (on every push to main and pull request) to
terminate EC2 instances created by the mngr_aws release tests that were left
behind when a test session was killed before its in-process cleanup
(``pytest_sessionfinish`` in ``libs/mngr_aws/imbue/mngr_aws/conftest.py``)
could run. It selects instances by the ``mngr-pytest-launched=true`` tag that
``AwsVpsClient.create_instance`` attaches to every pytest-launched instance and
terminates those whose EC2 ``LaunchTime`` is older than ``--max-age-hours``.
Production instances never carry that tag, so they are never touched.

Skips silently (exit 0) when AWS credentials cannot be resolved, so it is safe
to wire into CI before the secret is configured.

Usage:
    uv run python scripts/cleanup_old_aws_test_instances.py [--max-age-hours HOURS]

Options:
    --max-age-hours  Maximum age in hours for test instances to keep (default: 1.0)
"""

import argparse
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from imbue.imbue_common.logging import setup_logging
from imbue.mngr_aws.cleanup import cleanup_old_aws_test_instances
from imbue.mngr_aws.testing import aws_credentials_available
from imbue.mngr_aws.testing import make_aws_reaper_client


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean up old AWS test EC2 instances",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=1.0,
        help="Maximum age in hours for test instances to keep (default: 1.0)",
    )
    args = parser.parse_args()

    setup_logging(level="INFO")

    if not aws_credentials_available():
        print("AWS credentials could not be resolved; skipping AWS test instance cleanup")
        return 0

    cleaned_count = cleanup_old_aws_test_instances(
        make_aws_reaper_client(),
        max_age=timedelta(hours=args.max_age_hours),
        now=datetime.now(timezone.utc),
    )

    if cleaned_count > 0:
        print(f"Cleaned up {cleaned_count} old AWS test instance(s)")
    else:
        print("No old AWS test instances found to clean up")

    return 0


if __name__ == "__main__":
    sys.exit(main())
