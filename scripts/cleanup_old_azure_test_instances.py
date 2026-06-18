#!/usr/bin/env python3
"""Script to clean up old Azure test VMs (and orphaned NICs / public IPs).

This script is run by a CI job (on every push to main and pull request) to
delete Azure VMs created by the mngr_azure release tests that were left behind
when a test session was killed before its in-process cleanup
(``pytest_sessionfinish`` in ``libs/mngr_azure/imbue/mngr_azure/conftest.py``)
could run. It selects VMs by the ``mngr-pytest-launched=true`` tag that
``AzureVpsClient.create_instance`` attaches to every pytest-launched VM and
deletes those whose ``mngr-created-at`` tag is older than ``--max-age-hours``.
Production VMs never carry that tag, so they are never touched. Unattached
pytest-launched NICs / public IPs from capacity-failed creates are reclaimed
in the same pass.

Skips silently (exit 0) when Azure credentials or a subscription cannot be
resolved, so it is safe to wire into CI before the secret is configured.

Usage:
    uv run python scripts/cleanup_old_azure_test_instances.py [--max-age-hours HOURS]

Options:
    --max-age-hours  Maximum age in hours for test VMs to keep (default: 1.0)
"""

import argparse
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from azure.identity import DefaultAzureCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient

from imbue.imbue_common.logging import setup_logging
from imbue.mngr_azure.cleanup import cleanup_old_azure_test_instances
from imbue.mngr_azure.testing import AZURE_DEFAULT_RESOURCE_GROUP
from imbue.mngr_azure.testing import azure_credentials_available
from imbue.mngr_azure.testing import get_default_subscription_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean up old Azure test VMs",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=1.0,
        help="Maximum age in hours for test VMs to keep (default: 1.0)",
    )
    args = parser.parse_args()

    setup_logging(level="INFO")

    if not azure_credentials_available():
        print("Azure credentials could not be resolved; skipping Azure test VM cleanup")
        return 0
    subscription_id = get_default_subscription_id()
    if subscription_id is None:
        print("No Azure subscription could be resolved; skipping Azure test VM cleanup")
        return 0

    compute = ComputeManagementClient(DefaultAzureCredential(), subscription_id)
    network = NetworkManagementClient(DefaultAzureCredential(), subscription_id)
    cleaned_count = cleanup_old_azure_test_instances(
        compute,
        network,
        resource_group=AZURE_DEFAULT_RESOURCE_GROUP,
        max_age=timedelta(hours=args.max_age_hours),
        now=datetime.now(timezone.utc),
    )

    if cleaned_count > 0:
        print(f"Cleaned up {cleaned_count} old Azure test VM(s)")
    else:
        print("No old Azure test VMs found to clean up")

    return 0


if __name__ == "__main__":
    sys.exit(main())
