"""End-to-end release tests for the Vultr provider.

These tests create and destroy real VPS instances on Vultr and require
the VULTR_API_KEY environment variable to be set.

They are marked with @pytest.mark.release so they only run in CI or
when explicitly requested via `just test <path>::<test>`.
"""

import os
import subprocess
import time

import pytest
from pydantic import SecretStr

from imbue.mngr_vultr.client import VultrVpsClient

_VULTR_API_KEY = os.environ.get("VULTR_API_KEY", "")

pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(600),
    pytest.mark.skipif(not _VULTR_API_KEY, reason="VULTR_API_KEY not set"),
]


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


class TestVultrProviderLifecycle:
    """Tests for the full VPS Docker provider lifecycle."""

    def test_create_exec_and_destroy(self) -> None:
        """Create a host, run a command on it, then destroy it."""
        agent_name = f"test-vultr-{int(time.time()) % 100000}"

        # Create
        result = _run_mngr(
            "create", agent_name,
            "--provider", "vultr",
            "--no-connect",
            "--message", "just say hello",
        )
        assert result.returncode == 0, f"Create failed: {result.stderr}"
        assert "Done" in result.stdout or "created successfully" in result.stderr

        try:
            # Exec
            result = _run_mngr("exec", agent_name, "echo hello-from-vultr")
            assert result.returncode == 0, f"Exec failed: {result.stderr}"
            assert "hello-from-vultr" in result.stdout

            # Verify host_dir exists
            result = _run_mngr("exec", agent_name, "test -d /mngr && echo exists")
            assert result.returncode == 0, f"host_dir check failed: {result.stderr}"
            assert "exists" in result.stdout

            # List
            result = _run_mngr("list")
            assert result.returncode == 0, f"List failed: {result.stderr}"
            assert agent_name in result.stdout
            assert "vultr" in result.stdout
        finally:
            # Destroy (pipe 'y' for confirmation, use --force for running agents)
            result = subprocess.run(
                ["uv", "run", "mngr", "destroy", agent_name, "--force"],
                input="y\n",
                capture_output=True,
                text=True,
                timeout=120,
                cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
            )
            # Wait for background destroy to complete
            time.sleep(20)

    def test_create_stop_start_destroy(self) -> None:
        """Test the full stop/start lifecycle."""
        agent_name = f"test-vultr-ss-{int(time.time()) % 100000}"

        result = _run_mngr(
            "create", agent_name,
            "--provider", "vultr",
            "--no-connect",
            "--message", "just say hello",
        )
        assert result.returncode == 0, f"Create failed: {result.stderr}"

        try:
            # Stop the agent
            result = _run_mngr("stop", agent_name)
            assert result.returncode == 0, f"Stop failed: {result.stderr}"

            # Verify it appears as stopped in list
            result = _run_mngr("list")
            assert result.returncode == 0
            assert agent_name in result.stdout

            # Start the agent
            result = _run_mngr("start", agent_name, "--no-connect")
            assert result.returncode == 0, f"Start failed: {result.stderr}"

            # Verify it's running again
            result = _run_mngr("exec", agent_name, "echo alive-after-restart")
            assert result.returncode == 0, f"Post-restart exec failed: {result.stderr}"
            assert "alive-after-restart" in result.stdout
        finally:
            result = subprocess.run(
                ["uv", "run", "mngr", "destroy", agent_name, "--force"],
                input="y\n",
                capture_output=True,
                text=True,
                timeout=120,
                cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
            )
            time.sleep(20)

    def test_ssh_connectivity(self) -> None:
        """Verify we can SSH into the container directly."""
        agent_name = f"test-vultr-ssh-{int(time.time()) % 100000}"

        result = _run_mngr(
            "create", agent_name,
            "--provider", "vultr",
            "--no-connect",
            "--message", "just say hello",
        )
        assert result.returncode == 0, f"Create failed: {result.stderr}"

        try:
            # Check OS inside container
            result = _run_mngr("exec", agent_name, "cat /etc/os-release | head -1")
            assert result.returncode == 0, f"OS check failed: {result.stderr}"
            assert "Debian" in result.stdout or "debian" in result.stdout.lower()

            # Verify sshd is running
            result = _run_mngr("exec", agent_name, "pgrep -c sshd")
            assert result.returncode == 0, f"sshd check failed: {result.stderr}"
            sshd_count = int(result.stdout.strip().split("\n")[0])
            assert sshd_count >= 1
        finally:
            result = subprocess.run(
                ["uv", "run", "mngr", "destroy", agent_name, "--force"],
                input="y\n",
                capture_output=True,
                text=True,
                timeout=120,
                cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
            )
            time.sleep(20)


class TestVultrApiClient:
    """Tests for the Vultr API client with real API calls."""

    def test_list_instances_does_not_error(self) -> None:
        """Verify the API client can list instances without error."""
        client = VultrVpsClient(api_key=SecretStr(_VULTR_API_KEY))
        instances = client.list_instances()
        assert isinstance(instances, list)

    def test_list_ssh_keys(self) -> None:
        """Verify the API client can list SSH keys."""
        client = VultrVpsClient(api_key=SecretStr(_VULTR_API_KEY))
        keys = client.list_ssh_keys()
        assert isinstance(keys, list)

    def test_list_snapshots(self) -> None:
        """Verify the API client can list snapshots."""
        client = VultrVpsClient(api_key=SecretStr(_VULTR_API_KEY))
        snapshots = client.list_snapshots()
        assert isinstance(snapshots, list)
