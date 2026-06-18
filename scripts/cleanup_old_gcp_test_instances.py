#!/usr/bin/env python3
"""Script to clean up old GCP test GCE instances.

This script is run by a CI job (on every push to main and pull request) to
delete GCE instances created by the mngr_gcp release tests that were left
behind when a test session was killed before its in-process cleanup
(``pytest_sessionfinish`` in ``libs/mngr_gcp/imbue/mngr_gcp/conftest.py``)
could run. It selects instances by the ``mngr-pytest-launched=true`` label that
``GcpVpsClient.create_instance`` attaches to every pytest-launched instance and
deletes those whose ``creation_timestamp`` is older than ``--max-age-hours``.
Production instances never carry that label, so they are never touched.

Skips silently (exit 0) when GCP credentials or a default project cannot be
resolved, so it is safe to wire into CI before the secret is configured.

Usage:
    uv run python scripts/cleanup_old_gcp_test_instances.py [--max-age-hours HOURS]

Options:
    --max-age-hours  Maximum age in hours for test instances to keep (default: 1.0)
"""

import argparse
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from imbue.imbue_common.logging import setup_logging
from imbue.mngr_gcp.cleanup import cleanup_old_gcp_test_instances
from imbue.mngr_gcp.testing import gcp_credentials_available
from imbue.mngr_gcp.testing import get_default_project
from imbue.mngr_gcp.testing import make_gcp_reaper_client


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean up old GCP test GCE instances",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=1.0,
        help="Maximum age in hours for test instances to keep (default: 1.0)",
    )
    args = parser.parse_args()

    setup_logging(level="INFO")

    if not gcp_credentials_available():
        print("GCP Application Default Credentials could not be resolved; skipping GCP test instance cleanup")
        return 0
    project = get_default_project()
    if project is None:
        print("No default GCP project could be resolved; skipping GCP test instance cleanup")
        return 0

    cleaned_count = cleanup_old_gcp_test_instances(
        make_gcp_reaper_client(project),
        max_age=timedelta(hours=args.max_age_hours),
        now=datetime.now(timezone.utc),
    )

    if cleaned_count > 0:
        print(f"Cleaned up {cleaned_count} old GCP test instance(s)")
    else:
        print("No old GCP test instances found to clean up")

    return 0


if __name__ == "__main__":
    sys.exit(main())
