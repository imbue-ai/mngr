"""Pytest fixtures and session-finish leak detection for mngr_aws tests.

Modeled after ``libs/mngr_modal/imbue/mngr_modal/conftest.py``. Provides
the same three-layer safety net for AWS release tests so killed test
runs cannot leak EC2 cost:

1. Per-test cleanup happens via ``mngr destroy --force`` in each test's
   ``finally`` block (in ``test_release_aws.py``).
2. ``pytest_sessionfinish`` here scans for leaked EC2 instances tagged
   ``mngr-pytest-launched=true`` at the end of the session, force-
   terminates any matches older than the TTL, and fails the session.
3. Cloud-init in each test instance runs ``shutdown -P +N``. With the
   release default ``terminate_on_shutdown = true``
   (``InstanceInitiatedShutdownBehavior=terminate``) this self-terminates
   the instance even if pytest itself is killed; the resumable-idle-stop
   test overrides the flag to ``false`` (the cap stops, not terminates,
   the instance) and leans on layer 2 to reap any leak.

Layer 2 relies solely on a tag scan because the current release-test
path spawns ``mngr create`` in a subprocess, so there is no Python
hand-off back to the test process where an in-process tracking list
would live. ``AwsVpsClient.create_instance`` adds the
``mngr-pytest-launched=true`` tag (see ``AWS_PYTEST_LAUNCHED_TAG``)
to every EC2 instance launched while ``PYTEST_CURRENT_TEST`` is set,
and the scan filters on that tag and ignores anything younger than
``_TEST_LEAK_TTL`` so it never race-kills an in-flight test on a
parallel worker.

Also registers the shared plugin-test fixtures (including
``temp_mngr_ctx``) so backend-level unit tests can construct real
provider instances; mirrors the conftest pattern used by
``mngr_vps`` and ``mngr_schedule``.
"""

from collections.abc import Generator
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import boto3
import pytest
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from loguru import logger
from moto import mock_aws

from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures
from imbue.mngr.utils.testing import setup_mngr_test_environment
from imbue.mngr_aws.client import AWS_PYTEST_LAUNCHED_TAG
from imbue.mngr_aws.testing import AWS_DEFAULT_REGION
from imbue.mngr_aws.testing import AWS_RELEASE_TESTS_OPT_IN
from imbue.mngr_aws.testing import AWS_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS
from imbue.mngr_aws.testing import aws_credentials_available

register_plugin_test_fixtures(globals())


@pytest.fixture
def aws_session() -> Generator[boto3.Session, None, None]:
    """A boto3 Session with moto's in-memory AWS backend active and dummy creds.

    Shared by the unit tests that exercise S3/IAM behavior against moto (state
    bucket, host identity). Lives here so the moto context and the dummy-cred
    session have a single definition.
    """
    with mock_aws():
        yield boto3.Session(aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1")


@pytest.fixture
def aws_mock() -> Generator[None, None, None]:
    """Activate moto's in-memory AWS backend for a test that builds its own sessions."""
    with mock_aws():
        yield


@pytest.fixture(autouse=True)
def setup_test_mngr_env(
    tmp_home_dir: Path,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    mngr_test_root_name: str,
    monkeypatch: pytest.MonkeyPatch,
    _isolate_tmux_server: None,
) -> Generator[None, None, None]:
    """Override mngr's autouse env setup to snapshot AWS creds before HOME swap.

    Mirrors ``mngr_modal/conftest.py``'s analogous override: HOME isolation
    hides ``~/.aws/credentials`` and ``~/.aws/config`` from the test
    process, which makes boto3 raise ``NoCredentialsError`` even when the
    real shell has working creds. Resolve boto3's chain *before* HOME is
    swapped, then export the credentials as env vars so they survive
    isolation. Skipped silently when the session has no resolvable creds
    (the release-test ``skipif`` then takes over).
    """
    if aws_credentials_available():
        creds = boto3.Session().get_credentials()
        if creds is not None:
            frozen = creds.get_frozen_credentials()
            # boto3-stubs types these as ``str | None`` to allow the
            # not-yet-resolved case, but a frozen-credentials access_key
            # is non-empty by construction (None would have been caught
            # above via ``get_credentials() is None``).
            assert frozen.access_key and frozen.secret_key, "frozen credentials must not be empty"
            monkeypatch.setenv("AWS_ACCESS_KEY_ID", frozen.access_key)
            monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", frozen.secret_key)
            if frozen.token:
                monkeypatch.setenv("AWS_SESSION_TOKEN", frozen.token)
            # Stop boto3 inside the isolated HOME from re-reading the
            # config/credentials files (which won't exist there anyway).
            monkeypatch.delenv("AWS_PROFILE", raising=False)
            monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)
            monkeypatch.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
    setup_mngr_test_environment(tmp_home_dir, temp_host_dir, mngr_test_prefix, mngr_test_root_name, monkeypatch)
    yield


# Orphan-scan grace period. A test-named instance younger than this is left
# alone to avoid race-killing an in-flight test on a parallel worker.
# Derived from the shared ``AWS_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS`` constant
# (the same value release tests propagate into cloud-init) so the two TTLs
# can never drift.
_TEST_LEAK_TTL: Final[timedelta] = timedelta(seconds=AWS_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS)


def _find_orphan_test_instances(ec2: Any) -> list[str]:
    """Return instance IDs tagged ``mngr-pytest-launched=true`` and older than the TTL.

    ``AwsVpsClient.create_instance`` adds the tag to every EC2 instance
    launched while ``PYTEST_CURRENT_TEST`` is set, so this scanner only
    matches instances we created here. Younger instances are intentionally
    skipped so this never races a parallel worker's in-flight test.
    """
    cutoff = datetime.now(timezone.utc) - _TEST_LEAK_TTL
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


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Detect and clean up leaked AWS resources at session end.

    Implemented as a pytest hook (not a fixture) so it runs after every
    session-scoped fixture teardown, mirroring the Modal pattern. Skips
    silently unless release tests were actually opted into
    (``MNGR_AWS_RELEASE_TESTS=1``) and AWS credentials are available --
    the same conjunction that gates the release-test ``pytestmark``, so
    the cleanup hook never makes a live EC2 API call from a run that did
    not opt into AWS-using tests. If leaks are found, force-cleans them
    and sets ``session.exitstatus`` to ``TESTS_FAILED`` -- but only when
    the session was otherwise passing, so a more-specific failure
    (INTERRUPTED, INTERNAL_ERROR, etc.) is preserved.
    """
    # exitstatus is required by the hook signature but unused; we read
    # session.exitstatus, which is the canonical source.
    del exitstatus
    if not (AWS_RELEASE_TESTS_OPT_IN and aws_credentials_available()):
        return

    try:
        ec2 = boto3.Session(region_name=AWS_DEFAULT_REGION).client("ec2")
    except (BotoCoreError, ClientError) as e:
        logger.warning("Failed to build EC2 client for session-end leak scan: {}", e)
        return

    orphans = _find_orphan_test_instances(ec2)
    if not orphans:
        return

    try:
        ec2.terminate_instances(InstanceIds=orphans)
    except (BotoCoreError, ClientError) as e:
        logger.warning("Failed to terminate leaked EC2 instances {}: {}", orphans, e)
    message = (
        "=" * 70
        + "\nAWS SESSION CLEANUP FOUND LEAKED RESOURCES!\n"
        + "=" * 70
        + f"\n\nLeaked EC2 instances tagged {AWS_PYTEST_LAUNCHED_TAG}=true and "
        + f"older than {AWS_TEST_INSTANCE_AUTO_SHUTDOWN_SECONDS // 60} minutes:\n  "
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
