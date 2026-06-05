"""End-to-end release tests for the Vultr provider.

These tests create and destroy real VPS instances on Vultr and require
the VULTR_API_KEY environment variable to be set.

They are marked with @pytest.mark.release so they only run in CI or
when explicitly requested via `just test <path>::<test>`.
"""

import os
import subprocess
from uuid import uuid4

import pytest
from pydantic import SecretStr

from imbue.mngr.utils.polling import wait_for
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


def _destroy(agent_name: str) -> subprocess.CompletedProcess[str]:
    """Issue a forced destroy (pipe 'y' for the confirmation prompt)."""
    return subprocess.run(
        ["uv", "run", "mngr", "destroy", agent_name, "--force"],
        input="y\n",
        capture_output=True,
        text=True,
        timeout=120,
        cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
    )


def _destroy_and_confirm_gone(agent_name: str, timeout_seconds: float = 180.0) -> None:
    """Destroy the agent and poll `mngr list` until it disappears.

    Destruction is backgrounded, so we poll for the agent's absence rather
    than sleeping a fixed interval. A timeout here means the VPS may have
    leaked (a real, billed resource), so we surface it as a failure instead
    of silently passing.
    """
    result = _destroy(agent_name)
    assert result.returncode == 0, f"Destroy failed: {result.stderr}"
    wait_for(
        lambda: agent_name not in _run_mngr("list").stdout,
        timeout=timeout_seconds,
        poll_interval=5.0,
        error_message=(
            f"{agent_name} still present in `mngr list` after {timeout_seconds}s; destroy may have leaked a VPS"
        ),
    )


def _best_effort_destroy(agent_name: str) -> None:
    """Safety-net cleanup that never raises, so it cannot mask a test failure.

    Used in `finally` blocks: if the test body already failed (or never
    reached the asserted `_destroy_and_confirm_gone`), this still tears the
    VPS down without replacing the original exception.
    """
    try:
        _destroy(agent_name)
    except subprocess.SubprocessError:
        pass


def test_create_exec_and_destroy() -> None:
    """Create a host, run a command on it, then destroy it."""
    agent_name = f"test-vultr-{uuid4().hex}"

    result = _run_mngr(
        "create",
        agent_name,
        "--type",
        "claude",
        "--provider",
        "vultr",
        "--no-connect",
        "--message",
        "just say hello",
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

        _destroy_and_confirm_gone(agent_name)
    finally:
        _best_effort_destroy(agent_name)


def test_create_stop_start_destroy() -> None:
    """Test the full stop/start lifecycle."""
    agent_name = f"test-vultr-ss-{uuid4().hex}"

    result = _run_mngr(
        "create",
        agent_name,
        "--type",
        "claude",
        "--provider",
        "vultr",
        "--no-connect",
        "--message",
        "just say hello",
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

        _destroy_and_confirm_gone(agent_name)
    finally:
        _best_effort_destroy(agent_name)


def test_ssh_connectivity() -> None:
    """Verify we can SSH into the container directly."""
    agent_name = f"test-vultr-ssh-{uuid4().hex}"

    result = _run_mngr(
        "create",
        agent_name,
        "--type",
        "claude",
        "--provider",
        "vultr",
        "--no-connect",
        "--message",
        "just say hello",
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

        _destroy_and_confirm_gone(agent_name)
    finally:
        _best_effort_destroy(agent_name)


# The API-client tests below are intentionally minimal smoke checks: they hit
# the live Vultr account, whose contents are non-deterministic, so they assert
# only that listing succeeds and deserializes to a list rather than pinning
# specific values.


def test_list_instances_does_not_error() -> None:
    """Verify the API client can list instances without error."""
    client = VultrVpsClient(api_key=SecretStr(_VULTR_API_KEY))
    instances = client.list_instances()
    assert isinstance(instances, list)


def test_list_ssh_keys() -> None:
    """Verify the API client can list SSH keys."""
    client = VultrVpsClient(api_key=SecretStr(_VULTR_API_KEY))
    keys = client.list_ssh_keys()
    assert isinstance(keys, list)


def test_list_snapshots() -> None:
    """Verify the API client can list snapshots."""
    client = VultrVpsClient(api_key=SecretStr(_VULTR_API_KEY))
    snapshots = client.list_snapshots()
    assert isinstance(snapshots, list)
