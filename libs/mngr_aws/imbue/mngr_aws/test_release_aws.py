"""End-to-end release tests for the AWS provider.

These tests provision and destroy real EC2 instances on AWS. They cost
real money — typically a few cents per run for a ~5-minute t3.small —
and are double-gated:

- AWS credentials must be available -- specifically, either ``AWS_ACCESS_KEY_ID``
  or ``AWS_PROFILE`` must be set in the environment (this is the same probe
  used by the session-end cleanup hook; see ``testing.aws_credentials_available``).
- ``MNGR_AWS_RELEASE_TESTS=1`` must be set explicitly

Three layers of damage control prevent leaked EC2 cost (see
``conftest.py`` in this package for the full picture):

1. Each test's ``finally`` calls ``mngr destroy --force``.
2. ``pytest_sessionfinish`` in ``conftest.py`` force-terminates any
   instance still tagged with the test name prefix and older than the TTL
   at session end and fails the session.
3. The subprocess that runs ``mngr create`` is pointed at a temporary
   ``settings.toml`` (via ``MNGR_PROJECT_CONFIG_DIR``) that sets
   ``[providers.aws] auto_shutdown_minutes``. This propagates into
   cloud-init as ``shutdown -P +N`` on each test instance; combined with
   the launch flag ``InstanceInitiatedShutdownBehavior=terminate``, this
   auto-terminates the instance from the inside even if pytest itself
   is killed. The production AwsProvider refuses to create EC2 instances
   under pytest without this set, so a missed override fails closed.

Run manually:

    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
        MNGR_AWS_RELEASE_TESTS=1 \\
        just test libs/mngr_aws/imbue/mngr_aws/test_release_aws.py
"""

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest

from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.config import DEFAULT_AMI_BY_REGION
from imbue.mngr_aws.config import ExistingSecurityGroup
from imbue.mngr_aws.testing import AWS_DEFAULT_REGION
from imbue.mngr_aws.testing import AWS_RELEASE_TESTS_OPT_IN
from imbue.mngr_aws.testing import AWS_TEST_INSTANCE_AUTO_SHUTDOWN_MINUTES
from imbue.mngr_aws.testing import AWS_TEST_NAME_PREFIX
from imbue.mngr_aws.testing import aws_credentials_available

pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (aws_credentials_available() and AWS_RELEASE_TESTS_OPT_IN),
        reason="AWS credentials or MNGR_AWS_RELEASE_TESTS=1 not set",
    ),
]


@pytest.fixture()
def aws_test_settings_dir(tmp_path: Path) -> Iterator[Path]:
    """Write a project settings.toml that sets the AWS auto-shutdown TTL.

    The release tests must set ``auto_shutdown_minutes`` on the AWS
    provider config so the cloud-init self-shutdown safety net actually
    fires; the production AwsProvider refuses to create an EC2 instance
    under pytest without it. Using ``MNGR_PROJECT_CONFIG_DIR`` to point
    the subprocess at this settings file keeps the test-only TTL out of
    production code paths.

    ``MNGR_PROJECT_CONFIG_DIR`` is the literal directory containing
    ``settings.toml`` (see ``resolve_project_config_dir`` in
    ``mngr/config/pre_readers.py``); it is *not* a project root that
    gets a ``.<root_name>/`` subdirectory appended. So the file is
    written directly into ``tmp_path``.
    """
    (tmp_path / "settings.toml").write_text(
        f'[providers.aws]\nbackend = "aws"\nauto_shutdown_minutes = {AWS_TEST_INSTANCE_AUTO_SHUTDOWN_MINUTES}\n'
    )
    yield tmp_path


def _run_mngr(
    project_config_dir: Path,
    *args: str,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    """Run a mngr command with the test settings.toml in scope."""
    env = os.environ.copy()
    env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)
    cmd = ["uv", "run", "mngr", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
        env=env,
    )


# =============================================================================
# Provider lifecycle (full create / exec / stop / start / destroy)
# =============================================================================


def test_provider_lifecycle_create_exec_and_destroy(aws_test_settings_dir: Path) -> None:
    agent_name = f"{AWS_TEST_NAME_PREFIX}{int(time.time()) % 100000}"

    result = _run_mngr(
        aws_test_settings_dir,
        "create",
        agent_name,
        "--type",
        "claude",
        "--provider",
        "aws",
        "--no-connect",
        "--message",
        "just say hello",
    )
    assert result.returncode == 0, f"Create failed: {result.stderr}"
    assert "Done" in result.stdout or "created successfully" in result.stderr

    try:
        result = _run_mngr(aws_test_settings_dir, "exec", agent_name, "echo hello-from-aws")
        assert result.returncode == 0, f"Exec failed: {result.stderr}"
        assert "hello-from-aws" in result.stdout

        result = _run_mngr(aws_test_settings_dir, "exec", agent_name, "test -d /mngr && echo exists")
        assert result.returncode == 0, f"host_dir check failed: {result.stderr}"
        assert "exists" in result.stdout

        result = _run_mngr(aws_test_settings_dir, "list")
        assert result.returncode == 0, f"List failed: {result.stderr}"
        assert agent_name in result.stdout
        assert "aws" in result.stdout
    finally:
        # --force skips the destroy confirmation, so no stdin input needed.
        # Result is intentionally not checked: best-effort cleanup.
        _run_mngr(aws_test_settings_dir, "destroy", agent_name, "--force", timeout=120)
        time.sleep(20)


def test_provider_lifecycle_create_stop_start_destroy(aws_test_settings_dir: Path) -> None:
    agent_name = f"{AWS_TEST_NAME_PREFIX}ss-{int(time.time()) % 100000}"

    result = _run_mngr(
        aws_test_settings_dir,
        "create",
        agent_name,
        "--type",
        "claude",
        "--provider",
        "aws",
        "--no-connect",
        "--message",
        "just say hello",
    )
    assert result.returncode == 0, f"Create failed: {result.stderr}"

    try:
        result = _run_mngr(aws_test_settings_dir, "stop", agent_name)
        assert result.returncode == 0, f"Stop failed: {result.stderr}"

        result = _run_mngr(aws_test_settings_dir, "list")
        assert result.returncode == 0
        assert agent_name in result.stdout

        result = _run_mngr(aws_test_settings_dir, "start", agent_name, "--no-connect")
        assert result.returncode == 0, f"Start failed: {result.stderr}"

        result = _run_mngr(aws_test_settings_dir, "exec", agent_name, "echo alive-after-restart")
        assert result.returncode == 0, f"Post-restart exec failed: {result.stderr}"
        assert "alive-after-restart" in result.stdout
    finally:
        # --force skips the destroy confirmation, so no stdin input needed.
        # Result is intentionally not checked: best-effort cleanup.
        _run_mngr(aws_test_settings_dir, "destroy", agent_name, "--force", timeout=120)
        time.sleep(20)


# =============================================================================
# API client smoke tests (real network calls, read-only)
# =============================================================================


@pytest.fixture()
def aws_release_client() -> AwsVpsClient:
    """Real AWS API client for release-test read-only calls.

    Built with placeholder AMI / security-group IDs because the tests below
    only exercise read-only API operations (list_instances, list_ssh_keys,
    list_snapshots) that ignore those fields.
    """
    session = boto3.Session(region_name=AWS_DEFAULT_REGION)
    return AwsVpsClient(
        session=session,
        region=AWS_DEFAULT_REGION,
        ami_id="ami-placeholder",
        security_group=ExistingSecurityGroup(id="sg-placeholder"),
    )


def test_api_client_list_instances_does_not_error(aws_release_client: AwsVpsClient) -> None:
    instances = aws_release_client.list_instances()
    assert isinstance(instances, list)


def test_api_client_list_ssh_keys_does_not_error(aws_release_client: AwsVpsClient) -> None:
    keys = aws_release_client.list_ssh_keys()
    assert isinstance(keys, list)


def test_api_client_list_snapshots_does_not_error(aws_release_client: AwsVpsClient) -> None:
    snapshots = aws_release_client.list_snapshots()
    assert isinstance(snapshots, list)


def test_default_amis_describe_successfully() -> None:
    """Every entry in DEFAULT_AMI_BY_REGION must still resolve via DescribeImages.

    Hard-coded AMI IDs go stale over time -- Debian publishes new ones every
    few months and older snapshots eventually get deprecated. A periodic
    release-test run is the cheapest way to catch this: skipif gates the test
    on AWS credentials, so local runs without creds skip silently.
    """
    failures: list[str] = []
    for region, ami_id in DEFAULT_AMI_BY_REGION.items():
        ec2 = boto3.Session(region_name=region).client("ec2")
        response = ec2.describe_images(ImageIds=[ami_id])
        images = response.get("Images", [])
        if not images:
            failures.append(f"{region}: AMI {ami_id} not found")
            continue
        image = images[0]
        state = image.get("State", "")
        if state != "available":
            failures.append(f"{region}: AMI {ami_id} state={state!r} (expected 'available')")
    assert not failures, (
        "DEFAULT_AMI_BY_REGION has stale entries:\n  " + "\n  ".join(failures) + "\n"
        "Update the constant in libs/mngr_aws/imbue/mngr_aws/config.py with current "
        "Debian 12 amd64 AMI IDs from https://wiki.debian.org/Cloud/AmazonEC2Image."
    )
