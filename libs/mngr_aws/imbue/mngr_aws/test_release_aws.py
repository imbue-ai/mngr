"""End-to-end release tests for the AWS provider.

These tests provision and destroy real EC2 instances on AWS. They cost
real money — typically a few cents per run for a ~5-minute t3.small —
and are double-gated:

- AWS credentials must be available (env vars, profile, or instance role)
- ``MNGR_AWS_RELEASE_TESTS=1`` must be set explicitly

Three layers of damage control prevent leaked EC2 cost (see
``conftest.py`` in this package for the full picture):

1. Each test's ``finally`` calls ``mngr destroy --force``.
2. ``pytest_sessionfinish`` in ``conftest.py`` force-terminates any
   instance still tagged with the test name prefix and older than 1h
   at session end and fails the session.
3. ``MNGR_AWS_AUTO_SHUTDOWN_MINUTES=60`` is propagated into cloud-init,
   triggering ``shutdown -P +60`` on each test instance. Combined with
   ``InstanceInitiatedShutdownBehavior=terminate``, this auto-terminates
   the instance from the inside even if pytest itself is killed.

Run manually:

    AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... \\
        MNGR_AWS_RELEASE_TESTS=1 \\
        just test libs/mngr_aws/imbue/mngr_aws/test_release_aws.py
"""

import os
import subprocess
import time
from typing import Final

import boto3
import pytest

from imbue.mngr_aws.client import AwsVpsClient
from imbue.mngr_aws.constants import AWS_DEFAULT_REGION
from imbue.mngr_aws.constants import AWS_RELEASE_TESTS_OPT_IN
from imbue.mngr_aws.constants import AWS_TEST_NAME_PREFIX
from imbue.mngr_aws.testing import aws_credentials_available

_AWS_CREDS_PRESENT = aws_credentials_available()
# Belt-and-suspenders backstop against runaway EC2 cost: even if pytest is
# killed and the session-end leak detector never runs, this TTL drives cloud-init
# to schedule ``shutdown -P +N`` which (combined with the AWS launch flag
# ``InstanceInitiatedShutdownBehavior=terminate``) terminates the instance from
# the inside. 60 min is conservatively above any normal test run length.
_TEST_INSTANCE_AUTO_SHUTDOWN_MINUTES: Final[int] = 60

pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(900),
    pytest.mark.skipif(
        not (_AWS_CREDS_PRESENT and AWS_RELEASE_TESTS_OPT_IN),
        reason="AWS credentials or MNGR_AWS_RELEASE_TESTS=1 not set",
    ),
]


def _run_mngr(*args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    """Run a mngr command and return the result.

    Inherits the current process env, then forces ``MNGR_AWS_AUTO_SHUTDOWN_MINUTES``
    so every EC2 instance the test spins up has cloud-init schedule a
    ``shutdown -P +N``. Combined with the launch flag
    ``InstanceInitiatedShutdownBehavior=terminate``, this guarantees self-
    termination even if pytest is killed before the session-end cleanup
    hook in ``conftest.py`` runs.
    """
    env = os.environ.copy()
    env["MNGR_AWS_AUTO_SHUTDOWN_MINUTES"] = str(_TEST_INSTANCE_AUTO_SHUTDOWN_MINUTES)
    cmd = ["uv", "run", "mngr", *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
        env=env,
    )


class TestAwsProviderLifecycle:
    """Tests for the full EC2 Docker provider lifecycle."""

    def test_create_exec_and_destroy(self) -> None:
        agent_name = f"{AWS_TEST_NAME_PREFIX}{int(time.time()) % 100000}"

        result = _run_mngr(
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
            # --force skips the destroy confirmation, so no stdin input needed.
            # Result is intentionally not checked: best-effort cleanup.
            _run_mngr("destroy", agent_name, "--force", timeout=120)
            time.sleep(20)

    def test_create_stop_start_destroy(self) -> None:
        agent_name = f"{AWS_TEST_NAME_PREFIX}ss-{int(time.time()) % 100000}"

        result = _run_mngr(
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
            # --force skips the destroy confirmation, so no stdin input needed.
            # Result is intentionally not checked: best-effort cleanup.
            _run_mngr("destroy", agent_name, "--force", timeout=120)
            time.sleep(20)


class TestAwsApiClient:
    """Tests for the AWS API client with real EC2 API calls."""

    def _client(self) -> AwsVpsClient:
        session = boto3.Session(region_name=AWS_DEFAULT_REGION)
        return AwsVpsClient(
            session=session,
            region=AWS_DEFAULT_REGION,
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
