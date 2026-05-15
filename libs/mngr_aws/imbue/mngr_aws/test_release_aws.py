"""End-to-end release tests for the AWS provider.

These tests provision and destroy real EC2 instances on AWS. They cost
real money — typically a few cents per run for a ~5-minute t3.small —
and are double-gated:

- AWS credentials must be available (env vars, profile, or instance role)
- ``MNGR_AWS_RELEASE_TESTS=1`` must be set explicitly

A session-scoped autouse fixture force-terminates any leftover instances
matching the test naming convention older than 1 hour, providing a
backstop against leaked resources from previous runs.

Run manually:

    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
        MNGR_AWS_RELEASE_TESTS=1 \\
        just test libs/mngr_aws/imbue/mngr_aws/test_release_aws.py
"""

import os
import subprocess
import time
from collections.abc import Iterator
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import boto3
import pytest
from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError

from imbue.mngr_aws.client import AwsVpsClient

_AWS_CREDS_PRESENT = bool(os.environ.get("AWS_ACCESS_KEY_ID")) or bool(os.environ.get("AWS_PROFILE"))
_OPT_IN = os.environ.get("MNGR_AWS_RELEASE_TESTS") == "1"
_AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
_TEST_LEAK_TTL = timedelta(hours=1)
_TEST_NAME_PREFIX = "test-aws-"

pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (_AWS_CREDS_PRESENT and _OPT_IN),
        reason="AWS credentials or MNGR_AWS_RELEASE_TESTS=1 not set",
    ),
]


@pytest.fixture(scope="session", autouse=True)
def cleanup_leaked_instances() -> Iterator[None]:
    """After the session, force-terminate any leftover test instances older than 1h.

    Guards against leaked EC2 instances from previously-killed test runs. Only
    targets instances whose ``Name`` tag begins with the test-name prefix.
    """
    yield
    if not _OPT_IN:
        return
    try:
        session = boto3.Session(region_name=_AWS_REGION)
        ec2 = session.client("ec2", region_name=_AWS_REGION)
        cutoff = datetime.now(timezone.utc) - _TEST_LEAK_TTL
        leaked: list[str] = []
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate(
            Filters=[
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
                {"Name": "tag:Name", "Values": [f"mngr-{_TEST_NAME_PREFIX}*"]},
            ]
        ):
            for reservation in page.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    launch_time = instance.get("LaunchTime")
                    if isinstance(launch_time, datetime) and launch_time < cutoff:
                        leaked.append(instance["InstanceId"])
        if leaked:
            ec2.terminate_instances(InstanceIds=leaked)
    except (BotoCoreError, ClientError, KeyError) as e:
        pytest.fail(f"Leaked-instance cleanup failed: {e}")


def _run_mngr(*args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    """Run a mngr command and return the result."""
    cmd = ["uv", "run", "mngr", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
    )


class TestAwsProviderLifecycle:
    """Tests for the full EC2 Docker provider lifecycle."""

    def test_create_exec_and_destroy(self) -> None:
        agent_name = f"{_TEST_NAME_PREFIX}{int(time.time()) % 100000}"

        result = _run_mngr(
            "create",
            agent_name,
            "--provider",
            "aws",
            "--no-connect",
            "--message",
            "just say hello",
        )
        assert result.returncode == 0, f"Create failed: {result.stderr}"
        assert "Done" in result.stdout or "created successfully" in result.stderr

        try:
            result = _run_mngr("exec", agent_name, "echo hello-from-aws")
            assert result.returncode == 0, f"Exec failed: {result.stderr}"
            assert "hello-from-aws" in result.stdout

            result = _run_mngr("exec", agent_name, "test -d /mngr && echo exists")
            assert result.returncode == 0, f"host_dir check failed: {result.stderr}"
            assert "exists" in result.stdout

            result = _run_mngr("list")
            assert result.returncode == 0, f"List failed: {result.stderr}"
            assert agent_name in result.stdout
            assert "aws" in result.stdout
        finally:
            subprocess.run(
                ["uv", "run", "mngr", "destroy", agent_name, "--force"],
                input="y\n",
                capture_output=True,
                text=True,
                timeout=120,
                cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
            )
            time.sleep(20)

    def test_create_stop_start_destroy(self) -> None:
        agent_name = f"{_TEST_NAME_PREFIX}ss-{int(time.time()) % 100000}"

        result = _run_mngr(
            "create",
            agent_name,
            "--provider",
            "aws",
            "--no-connect",
            "--message",
            "just say hello",
        )
        assert result.returncode == 0, f"Create failed: {result.stderr}"

        try:
            result = _run_mngr("stop", agent_name)
            assert result.returncode == 0, f"Stop failed: {result.stderr}"

            result = _run_mngr("list")
            assert result.returncode == 0
            assert agent_name in result.stdout

            result = _run_mngr("start", agent_name, "--no-connect")
            assert result.returncode == 0, f"Start failed: {result.stderr}"

            result = _run_mngr("exec", agent_name, "echo alive-after-restart")
            assert result.returncode == 0, f"Post-restart exec failed: {result.stderr}"
            assert "alive-after-restart" in result.stdout
        finally:
            subprocess.run(
                ["uv", "run", "mngr", "destroy", agent_name, "--force"],
                input="y\n",
                capture_output=True,
                text=True,
                timeout=120,
                cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
            )
            time.sleep(20)


class TestAwsApiClient:
    """Tests for the AWS API client with real EC2 API calls."""

    def _client(self) -> AwsVpsClient:
        session = boto3.Session(region_name=_AWS_REGION)
        return AwsVpsClient(
            session=session,
            region=_AWS_REGION,
            ami_id="ami-placeholder",
            security_group_id="sg-placeholder",
        )

    def test_list_instances_does_not_error(self) -> None:
        client = self._client()
        instances = client.list_instances()
        assert isinstance(instances, list)

    def test_list_ssh_keys_does_not_error(self) -> None:
        client = self._client()
        keys = client.list_ssh_keys()
        assert isinstance(keys, list)

    def test_list_snapshots_does_not_error(self) -> None:
        client = self._client()
        snapshots = client.list_snapshots()
        assert isinstance(snapshots, list)
