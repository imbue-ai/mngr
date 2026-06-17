"""End-to-end release tests for the Vultr provider.

These tests create and destroy real VPS instances on Vultr and require
the VULTR_API_KEY environment variable to be set.

They are marked with @pytest.mark.release so they only run in CI or
when explicitly requested via `just test <path>::<test>`.
"""

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.mngr_vultr.client import VultrVpsClient

_VULTR_API_KEY = os.environ.get("VULTR_API_KEY", "")

pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(600),
    pytest.mark.skipif(not _VULTR_API_KEY, reason="VULTR_API_KEY not set"),
]


@pytest.fixture()
def vultr_test_settings_dir(tmp_path: Path) -> Iterator[Path]:
    """Write a project settings.toml that opts into pytest and selects Vultr.

    The ``mngr create`` subprocess inherits ``PYTEST_CURRENT_TEST`` and refuses
    to load any config that does not set ``is_allowed_in_pytest = true``.
    Pointing the subprocess at this temp config via ``MNGR_PROJECT_CONFIG_DIR``
    keeps the opt-in out of the developer's real config and selects the Vultr
    provider (the API key comes from ``VULTR_API_KEY`` in the environment;
    provider defaults supply region / plan / OS id).
    """
    (tmp_path / "settings.toml").write_text(
        # Top-level key, so it must precede the first table.
        "is_allowed_in_pytest = true\n"
        "\n[providers.vultr]\n"
        'backend = "vultr"\n'
        # Disable other remote providers so the create-host preflight doesn't
        # trip looking for their credentials.
        "\n[providers.modal]\nis_enabled = false\n"
        "\n[providers.azure]\nis_enabled = false\n"
        "\n[providers.gcp]\nis_enabled = false\n"
        "\n[providers.aws]\nis_enabled = false\n"
        "\n[providers.ovh]\nis_enabled = false\n"
        "\n[providers.imbue_cloud]\nis_enabled = false\n"
    )
    yield tmp_path


def _run_mngr(project_config_dir: Path, *args: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Run a mngr command with the test settings.toml in scope.

    The default timeout is generous because ``create`` provisions a real VPS:
    Vultr provisioning alone can take ~90s, and on a slow run the full
    create (provision + cloud-init + Docker build + rsync) intermittently
    exceeded a tighter 300s budget, failing the test with a spurious
    ``subprocess.TimeoutExpired`` rather than a real defect.
    """
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


def _destroy(project_config_dir: Path, agent_name: str) -> None:
    """Force-destroy an agent with the test settings.toml in scope."""
    env = os.environ.copy()
    env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)
    subprocess.run(
        ["uv", "run", "mngr", "destroy", agent_name, "--force"],
        input="y\n",
        capture_output=True,
        text=True,
        timeout=120,
        cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
        env=env,
    )
    # Wait for background destroy to complete.
    time.sleep(20)


class TestVultrProviderLifecycle:
    """Tests for the full VPS Docker provider lifecycle."""

    @pytest.mark.rsync
    def test_create_exec_and_destroy(self, vultr_test_settings_dir: Path) -> None:
        """Create a host, run a command on it, then destroy it."""
        agent_name = f"test-vultr-{int(time.time()) % 100000}"

        # Create (uses rsync to upload the build context to the VPS)
        result = _run_mngr(
            vultr_test_settings_dir,
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
            result = _run_mngr(vultr_test_settings_dir, "exec", agent_name, "echo hello-from-vultr")
            assert result.returncode == 0, f"Exec failed: {result.stderr}"
            assert "hello-from-vultr" in result.stdout

            # Verify host_dir exists
            result = _run_mngr(vultr_test_settings_dir, "exec", agent_name, "test -d /mngr && echo exists")
            assert result.returncode == 0, f"host_dir check failed: {result.stderr}"
            assert "exists" in result.stdout

            # List
            result = _run_mngr(vultr_test_settings_dir, "list")
            assert result.returncode == 0, f"List failed: {result.stderr}"
            assert agent_name in result.stdout
            assert "vultr" in result.stdout
        finally:
            _destroy(vultr_test_settings_dir, agent_name)

    @pytest.mark.rsync
    def test_create_stop_start_destroy(self, vultr_test_settings_dir: Path) -> None:
        """Test the full stop/start lifecycle."""
        agent_name = f"test-vultr-ss-{int(time.time()) % 100000}"

        result = _run_mngr(
            vultr_test_settings_dir,
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
            result = _run_mngr(vultr_test_settings_dir, "stop", agent_name)
            assert result.returncode == 0, f"Stop failed: {result.stderr}"

            # Verify it appears as stopped in list
            result = _run_mngr(vultr_test_settings_dir, "list")
            assert result.returncode == 0
            assert agent_name in result.stdout

            # Start the agent
            result = _run_mngr(vultr_test_settings_dir, "start", agent_name, "--no-connect")
            assert result.returncode == 0, f"Start failed: {result.stderr}"

            # Verify it's running again
            result = _run_mngr(vultr_test_settings_dir, "exec", agent_name, "echo alive-after-restart")
            assert result.returncode == 0, f"Post-restart exec failed: {result.stderr}"
            assert "alive-after-restart" in result.stdout
        finally:
            _destroy(vultr_test_settings_dir, agent_name)

    @pytest.mark.rsync
    def test_ssh_connectivity(self, vultr_test_settings_dir: Path) -> None:
        """Verify we can SSH into the container directly."""
        agent_name = f"test-vultr-ssh-{int(time.time()) % 100000}"

        result = _run_mngr(
            vultr_test_settings_dir,
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
            result = _run_mngr(vultr_test_settings_dir, "exec", agent_name, "cat /etc/os-release | head -1")
            assert result.returncode == 0, f"OS check failed: {result.stderr}"
            assert "Debian" in result.stdout or "debian" in result.stdout.lower()

            # Verify sshd is running
            result = _run_mngr(vultr_test_settings_dir, "exec", agent_name, "pgrep -c sshd")
            assert result.returncode == 0, f"sshd check failed: {result.stderr}"
            sshd_count = int(result.stdout.strip().split("\n")[0])
            assert sshd_count >= 1
        finally:
            _destroy(vultr_test_settings_dir, agent_name)


@pytest.fixture()
def vultr_release_client() -> VultrVpsClient:
    """Real Vultr API client for release-test read-only calls."""
    return VultrVpsClient(api_key=SecretStr(_VULTR_API_KEY), os_id=2136)


class TestVultrApiClient:
    """Tests for the Vultr API client with real API calls."""

    def test_list_instances_does_not_error(self, vultr_release_client: VultrVpsClient) -> None:
        """Verify the API client can list instances without error."""
        instances = vultr_release_client.list_instances()
        assert isinstance(instances, list)
