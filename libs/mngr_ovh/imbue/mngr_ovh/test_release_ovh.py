"""End-to-end release tests for the OVH provider.

These tests create and destroy real OVH VPS instances and require OVH
credentials (any of OAuth2 OR application key + secret + consumer key,
or a populated ``~/.ovh.conf``).

They are marked with @pytest.mark.release so they only run in CI or
when explicitly requested via ``just test <path>::<test>``.

Because OVH bills monthly, ``destroy_host`` forfeits the prorated
remainder of the month -- so we keep this test suite small and serialize
it via a per-test name suffix to make collisions visible if anything is
left dangling.
"""

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import ovh
import pytest

from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.config import OvhProviderConfig


def _has_ovh_credentials() -> bool:
    """True iff env vars or ``~/.ovh.conf`` provide enough to authenticate."""
    config = OvhProviderConfig()
    if config.has_explicit_credentials():
        return True
    home_conf = os.path.expanduser("~/.ovh.conf")
    return os.path.isfile(home_conf)


_HAS_OVH = _has_ovh_credentials()

pytestmark = [
    pytest.mark.release,
    pytest.mark.timeout(1800),
    pytest.mark.skipif(not _HAS_OVH, reason="OVH credentials not configured"),
]


@pytest.fixture()
def ovh_test_settings_dir(tmp_path: Path) -> Iterator[Path]:
    """Write a project settings.toml that opts into pytest and selects OVH.

    The ``mngr create`` subprocess inherits ``PYTEST_CURRENT_TEST`` and refuses
    to load any config that does not set ``is_allowed_in_pytest = true``.
    Pointing the subprocess at this temp config via ``MNGR_PROJECT_CONFIG_DIR``
    keeps the opt-in out of the developer's real config and selects the OVH
    provider (credentials come from the ``OVH_*`` env vars or ``~/.ovh.conf``;
    provider defaults supply region / plan / image).
    """
    (tmp_path / "settings.toml").write_text(
        # Top-level key, so it must precede the first table.
        "is_allowed_in_pytest = true\n"
        "\n[providers.ovh]\n"
        'backend = "ovh"\n'
        # Disable other remote providers so the create-host preflight doesn't
        # trip looking for their credentials.
        "\n[providers.modal]\nis_enabled = false\n"
        "\n[providers.azure]\nis_enabled = false\n"
        "\n[providers.gcp]\nis_enabled = false\n"
        "\n[providers.aws]\nis_enabled = false\n"
        "\n[providers.vultr]\nis_enabled = false\n"
        "\n[providers.imbue_cloud]\nis_enabled = false\n"
    )
    yield tmp_path


def _run_mngr(project_config_dir: Path, *args: str, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
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
        timeout=300,
        cwd=os.environ.get("MNGR_REPO_ROOT", os.getcwd()),
        env=env,
    )
    time.sleep(30)


def _build_client() -> OvhVpsClient:
    config = OvhProviderConfig()
    raw = ovh.Client(**config.resolve_python_ovh_kwargs())
    return OvhVpsClient(ovh_client=raw, subsidiary=config.ovh_subsidiary)


class TestOvhProviderLifecycle:
    """End-to-end create/exec/destroy through the mngr CLI."""

    @pytest.mark.rsync
    def test_create_exec_and_destroy(self, ovh_test_settings_dir: Path) -> None:
        agent_name = f"test-ovh-{int(time.time()) % 100000}"

        # Create (uses rsync to upload the build context to the VPS)
        result = _run_mngr(
            ovh_test_settings_dir,
            "create",
            agent_name,
            "--type",
            "claude",
            "--provider",
            "ovh",
            "--no-connect",
            "--message",
            "just say hello",
        )
        assert result.returncode == 0, f"Create failed: {result.stderr}"

        try:
            result = _run_mngr(ovh_test_settings_dir, "exec", agent_name, "echo hello-from-ovh")
            assert result.returncode == 0, f"Exec failed: {result.stderr}"
            assert "hello-from-ovh" in result.stdout

            result = _run_mngr(ovh_test_settings_dir, "exec", agent_name, "test -d /mngr && echo exists")
            assert result.returncode == 0, f"host_dir check failed: {result.stderr}"
            assert "exists" in result.stdout

            result = _run_mngr(ovh_test_settings_dir, "list")
            assert result.returncode == 0, f"List failed: {result.stderr}"
            assert agent_name in result.stdout
            assert "ovh" in result.stdout
        finally:
            _destroy(ovh_test_settings_dir, agent_name)

    @pytest.mark.rsync
    def test_create_stop_start_destroy(self, ovh_test_settings_dir: Path) -> None:
        agent_name = f"test-ovh-ss-{int(time.time()) % 100000}"

        result = _run_mngr(
            ovh_test_settings_dir,
            "create",
            agent_name,
            "--type",
            "claude",
            "--provider",
            "ovh",
            "--no-connect",
            "--message",
            "just say hello",
        )
        assert result.returncode == 0, f"Create failed: {result.stderr}"

        try:
            result = _run_mngr(ovh_test_settings_dir, "stop", agent_name)
            assert result.returncode == 0, f"Stop failed: {result.stderr}"

            result = _run_mngr(ovh_test_settings_dir, "list")
            assert result.returncode == 0
            assert agent_name in result.stdout

            result = _run_mngr(ovh_test_settings_dir, "start", agent_name, "--no-connect")
            assert result.returncode == 0, f"Start failed: {result.stderr}"

            result = _run_mngr(ovh_test_settings_dir, "exec", agent_name, "echo alive-after-restart")
            assert result.returncode == 0, f"Post-restart exec failed: {result.stderr}"
            assert "alive-after-restart" in result.stdout
        finally:
            _destroy(ovh_test_settings_dir, agent_name)


class TestOvhVpsClient:
    """Read-only smoke tests against the live OVH API."""

    def test_list_instances_does_not_error(self) -> None:
        client = _build_client()
        assert isinstance(client.list_instances(), list)
