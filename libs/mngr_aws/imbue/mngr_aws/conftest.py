"""Pytest fixtures and session-finish leak detection for mngr_aws tests.

Modeled after ``libs/mngr_modal/imbue/mngr_modal/conftest.py``. Provides
the same three-layer safety net for AWS release tests so killed test
runs cannot leak EC2 cost:

1. Per-test cleanup happens via ``mngr destroy --force`` in each test's
   ``finally`` block (in ``test_release_aws.py``).
2. ``pytest_sessionfinish`` here detects any leftover resources at the
   end of the session, force-cleans them, and fails the session.
3. Cloud-init in each test instance runs ``shutdown -P +N`` with
   ``InstanceInitiatedShutdownBehavior=terminate``, which self-
   terminates the instance even if pytest itself is killed.

Layer 2's tracked-resource lists (``worker_aws_test_instance_ids`` /
``worker_aws_test_keypair_names``) are scoped per pytest-xdist worker
process. Tests that create resources in-process via the SDK should call
``register_aws_test_*`` after a successful create and
``deregister_aws_test_*`` after a confirmed delete. The hook also scans
for orphans by ``Name`` tag prefix as a backstop for resources that
escape tracking (e.g. created via subprocess where there is no Python
hand-off to register them).
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

# Test-resource tracking lists scoped to this worker process (xdist-safe).
# Each entry is added by ``register_aws_test_*`` when a resource is created
# and removed by ``deregister_aws_test_*`` after the resource is confirmed
# gone. Anything still in these lists at session end is reported as a leak.
worker_aws_test_instance_ids: list[str] = []
worker_aws_test_keypair_names: list[str] = []

# Region used by the session-end leak scan. Tests can override via
# ``AWS_REGION``; defaults to ``us-east-1`` to match the rest of the suite.
_AWS_REGION: Final[str] = os.environ.get("AWS_REGION", "us-east-1")

# Orphan-scan grace period. An untracked test-named instance younger than
# this is left alone to avoid race-killing an in-flight test on a parallel
# worker. Aligned with the ``MNGR_AWS_AUTO_SHUTDOWN_MINUTES=60`` cap that
# release tests propagate into cloud-init.
_TEST_LEAK_TTL: Final[timedelta] = timedelta(hours=1)


def register_aws_test_instance(instance_id: str) -> None:
    """Track an EC2 instance for end-of-session leak detection.

    Call after a successful ``RunInstances`` in test code. The session-end
    hook will force-terminate anything still tracked at the end of the run
    and fail the session.
    """
    if instance_id not in worker_aws_test_instance_ids:
        worker_aws_test_instance_ids.append(instance_id)


def deregister_aws_test_instance(instance_id: str) -> None:
    """Stop tracking an instance after a confirmed terminate."""
    if instance_id in worker_aws_test_instance_ids:
        worker_aws_test_instance_ids.remove(instance_id)


def register_aws_test_keypair(name: str) -> None:
    """Track an EC2 KeyPair name for end-of-session leak detection."""
    if name not in worker_aws_test_keypair_names:
        worker_aws_test_keypair_names.append(name)


def deregister_aws_test_keypair(name: str) -> None:
    """Stop tracking a KeyPair name after a confirmed delete."""
    if name in worker_aws_test_keypair_names:
        worker_aws_test_keypair_names.remove(name)


def _aws_credentials_available() -> bool:
    return bool(os.environ.get("AWS_ACCESS_KEY_ID")) or bool(os.environ.get("AWS_PROFILE"))


def _force_terminate_instances(ec2: Any, instance_ids: list[str]) -> None:
    if not instance_ids:
        return
    try:
        ec2.terminate_instances(InstanceIds=instance_ids)
    except (BotoCoreError, ClientError) as e:
        logger.warning("Failed to terminate leaked EC2 instances {}: {}", instance_ids, e)


def _force_delete_keypairs(ec2: Any, names: list[str]) -> None:
    for name in names:
        try:
            ec2.delete_key_pair(KeyName=name)
        except (BotoCoreError, ClientError) as e:
            logger.warning("Failed to delete leaked EC2 KeyPair {}: {}", name, e)


def _find_orphan_instances_by_name(ec2: Any) -> list[str]:
    """Return instance IDs whose ``Name`` tag begins with the test prefix and are older than the TTL.

    Backstop for instances that escaped the tracked-resource list. Younger
    instances are intentionally skipped so this never races a parallel
    worker's in-flight test.
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
    if not (worker_aws_test_instance_ids or worker_aws_test_keypair_names):
        return

    try:
        ec2 = boto3.Session(region_name=_AWS_REGION).client("ec2", region_name=_AWS_REGION)
    except (BotoCoreError, ClientError) as e:
        logger.warning("Failed to build EC2 client for session-end leak scan: {}", e)
        return

    errors: list[str] = []
    if worker_aws_test_instance_ids:
        errors.append("Leftover EC2 instances tracked:\n  " + "\n  ".join(worker_aws_test_instance_ids))
        _force_terminate_instances(ec2, list(worker_aws_test_instance_ids))
    orphans = _find_orphan_instances_by_name(ec2)
    if orphans:
        errors.append(
            "Untracked EC2 instances matching the test name prefix and older than 1h:\n  " + "\n  ".join(orphans)
        )
        _force_terminate_instances(ec2, orphans)
    if worker_aws_test_keypair_names:
        errors.append("Leftover EC2 KeyPairs tracked:\n  " + "\n  ".join(worker_aws_test_keypair_names))
        _force_delete_keypairs(ec2, list(worker_aws_test_keypair_names))

    if errors:
        message = (
            "=" * 70
            + "\nAWS SESSION CLEANUP FOUND LEAKED RESOURCES!\n"
            + "=" * 70
            + "\n\n"
            + "\n\n".join(errors)
            + "\n\nResources have been force-cleaned, but tests should not leak.\n"
        )
        logger.error(message)
        # Force the test session to fail. Only overwrite a successful
        # status: a non-zero status (INTERRUPTED=2, INTERNAL_ERROR=3,
        # USAGE_ERROR=4, NO_TESTS_COLLECTED=5) carries strictly more
        # diagnostic information than TESTS_FAILED=1.
        if session.exitstatus == 0:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
