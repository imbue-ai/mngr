"""Out-of-band, age-based reaper for leaked AWS test EC2 instances.

The session-end leak check in ``conftest.py`` only reaps instances when the
pytest session survives to run ``pytest_sessionfinish``. A session/runner
killed mid-run leaks instances that only the *next* AWS release session would
reap -- and the release suite is not run in CI, so that next run may be far
off. This module is the account-wide, age-based reaper that the standalone CI
job (``scripts/cleanup_old_aws_test_instances.py``, run on every push to main
and pull request) drives, mirroring Modal's
``cleanup_old_modal_test_environments`` and Vultr's
``cleanup_old_vultr_test_instances``.

``AwsVpsClient.create_instance`` tags every EC2 instance launched while
``PYTEST_CURRENT_TEST`` is set with ``mngr-pytest-launched=true``. The reaper
matches on that tag -- so production instances, which never carry it, are never
touched -- and keeps only those whose EC2 ``LaunchTime`` is older than a max
age. The same ``find_old_test_instances`` / ``terminate_test_instances``
functions back the ``conftest.py`` session-end check, so the two paths can
never drift.
"""

from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from typing import Any

from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from loguru import logger

from imbue.mngr_aws.client import AWS_PYTEST_LAUNCHED_TAG


def find_old_test_instances(ec2: Any, max_age: timedelta, now: datetime) -> list[str]:
    """Return IDs of ``mngr-pytest-launched=true`` EC2 instances older than ``max_age``.

    Filters server-side on the pytest-launched tag, then keeps only instances
    whose ``LaunchTime`` is before ``now - max_age``. Younger instances are
    skipped so neither the session-end check nor the reaper ever race-kills an
    in-flight test on a parallel worker. A scan failure logs and yields an
    empty list rather than raising, so a transient API error never aborts the
    caller.
    """
    cutoff = now - max_age
    leaked: list[str] = []
    try:
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate(
            Filters=[
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
                {"Name": f"tag:{AWS_PYTEST_LAUNCHED_TAG}", "Values": ["true"]},
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


def terminate_test_instances(ec2: Any, instance_ids: Sequence[str]) -> None:
    """Best-effort terminate of the given EC2 instances; logs and swallows failures.

    Does not raise: the goal is to clean up as many leaked instances as
    possible, and a failed terminate is logged so a stuck reaper is greppable
    in the CI logs that drive this script.
    """
    if not instance_ids:
        return
    try:
        ec2.terminate_instances(InstanceIds=list(instance_ids))
    except (BotoCoreError, ClientError) as e:
        logger.warning("Failed to terminate leaked EC2 instances {}: {}", list(instance_ids), e)


def cleanup_old_aws_test_instances(ec2: Any, max_age: timedelta, now: datetime) -> int:
    """Terminate ``mngr-pytest-launched`` EC2 instances older than ``max_age``; return the count.

    Never raises: scan failures yield an empty list and terminate failures are
    logged and swallowed. The returned count is the number of instances
    targeted for termination.
    """
    old = find_old_test_instances(ec2, max_age, now)
    if not old:
        logger.info("No leaked AWS test instances older than {} found", max_age)
        return 0
    logger.info("Found {} leaked AWS test instance(s) older than {}; terminating", len(old), max_age)
    terminate_test_instances(ec2, old)
    return len(old)
