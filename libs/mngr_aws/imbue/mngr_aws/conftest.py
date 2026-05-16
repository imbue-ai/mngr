"""Pytest fixtures and session-finish leak detection for mngr_aws tests.

Modeled after ``libs/mngr_modal/imbue/mngr_modal/conftest.py``. Provides
the same three-layer safety net for AWS release tests so killed test
runs cannot leak EC2 cost:

1. Per-test cleanup happens via ``mngr destroy --force`` in each test's
   ``finally`` block (in ``test_release_aws.py``).
2. ``pytest_sessionfinish`` here scans for leaked EC2 instances by
   ``Name`` tag prefix at the end of the session, force-terminates any
   matches older than the TTL, and fails the session.
3. Cloud-init in each test instance runs ``shutdown -P +N`` with
   ``InstanceInitiatedShutdownBehavior=terminate``, which self-
   terminates the instance even if pytest itself is killed.

Layer 2 relies solely on a ``Name``-tag scan because the current
release-test path spawns ``mngr create`` in a subprocess, so there is
no Python hand-off back to the test process where an in-process
tracking list would live. The scan is naturally tag-based (matching
``mngr-<AWS_TEST_NAME_PREFIX>*``) and ignores anything younger than
``_TEST_LEAK_TTL`` so it never race-kills an in-flight test on a
parallel worker.
"""

from __future__ import annotations

import os
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Final

import boto3
import pytest
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from loguru import logger

from imbue.mngr_aws.constants import AWS_TEST_NAME_PREFIX

# Region used by the session-end leak scan. Tests can override via
# ``AWS_REGION``; defaults to ``us-east-1`` to match the rest of the suite.
_AWS_REGION: Final[str] = os.environ.get("AWS_REGION", "us-east-1")

# Orphan-scan grace period. An untracked test-named instance younger than
# this is left alone to avoid race-killing an in-flight test on a parallel
# worker. Aligned with the ``MNGR_AWS_AUTO_SHUTDOWN_MINUTES=60`` cap that
# release tests propagate into cloud-init.
_TEST_LEAK_TTL: Final[timedelta] = timedelta(hours=1)


def _aws_credentials_available() -> bool:
    return bool(os.environ.get("AWS_ACCESS_KEY_ID")) or bool(os.environ.get("AWS_PROFILE"))


def _force_terminate_instances(ec2: Any, instance_ids: list[str]) -> None:
    if not instance_ids:
        return
    try:
        ec2.terminate_instances(InstanceIds=instance_ids)
    except (BotoCoreError, ClientError) as e:
        logger.warning("Failed to terminate leaked EC2 instances {}: {}", instance_ids, e)


def _find_orphan_instances_by_name(ec2: Any) -> list[str]:
    """Return instance IDs whose ``Name`` tag begins with the test prefix and are older than the TTL.

    Younger instances are intentionally skipped so this never races a
    parallel worker's in-flight test.
    """
    cutoff = datetime.now(timezone.utc) - _TEST_LEAK_TTL
    leaked: list[str] = []
    try:
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate(
            Filters=[
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
                {"Name": "tag:Name", "Values": [f"mngr-{AWS_TEST_NAME_PREFIX}*"]},
            ]
        ):
            for reservation in page.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    launch_time = instance.get("LaunchTime")
                    if isinstance(launch_time, datetime) and launch_time < cutoff:
                        leaked.append(instance["InstanceId"])
    except (BotoCoreError, ClientError) as e:
        logger.warning("Failed to scan for orphaned EC2 test instances: {}", e)
    return leaked


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Detect and clean up leaked AWS resources at session end.

    Implemented as a pytest hook (not a fixture) so it runs after every
    session-scoped fixture teardown, mirroring the Modal pattern. Skips
    silently when AWS credentials are unavailable (release tests gated
    off). If leaks are found, force-cleans them and sets
    ``session.exitstatus`` to ``TESTS_FAILED`` -- but only when the
    session was otherwise passing, so a more-specific failure
    (INTERRUPTED, INTERNAL_ERROR, etc.) is preserved.
    """
    # exitstatus is required by the hook signature but unused; we read
    # session.exitstatus, which is the canonical source.
    del exitstatus
    if not _aws_credentials_available():
        return

    try:
        ec2 = boto3.Session(region_name=_AWS_REGION).client("ec2", region_name=_AWS_REGION)
    except (BotoCoreError, ClientError) as e:
        logger.warning("Failed to build EC2 client for session-end leak scan: {}", e)
        return

    orphans = _find_orphan_instances_by_name(ec2)
    if not orphans:
        return

    _force_terminate_instances(ec2, orphans)
    message = (
        "=" * 70
        + "\nAWS SESSION CLEANUP FOUND LEAKED RESOURCES!\n"
        + "=" * 70
        + "\n\nUntracked EC2 instances matching the test name prefix and older than 1h:\n  "
        + "\n  ".join(orphans)
        + "\n\nInstances have been force-terminated, but tests should not leak.\n"
    )
    logger.error(message)
    # Force the test session to fail. Only overwrite a successful
    # status: a non-zero status (INTERRUPTED=2, INTERNAL_ERROR=3,
    # USAGE_ERROR=4, NO_TESTS_COLLECTED=5) carries strictly more
    # diagnostic information than TESTS_FAILED=1.
    if session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
