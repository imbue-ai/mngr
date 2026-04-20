"""Acceptance tests for SSHPasswordAuth against a real sshd running in Docker.

These tests verify the end-to-end transport produced by build_transport_command
actually authenticates against a real sshd with password auth enabled. They
complement the unit tests in auth_test.py, which only check the command string
structure.

Requires:
- Docker daemon running (@pytest.mark.docker_sdk)
- sshpass installed on the test runner (skipped otherwise)
"""

import os
import shutil
import subprocess
from collections.abc import Generator
from pathlib import Path

import docker
import docker.errors
import docker.models.containers
import pytest
from pydantic import SecretStr

from imbue.mngr.interfaces.ssh_auth import expose_secrets_for_subprocess
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr_ssh_password_auth.auth import SSHPasswordAuth

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.docker_sdk,
    pytest.mark.skipif(shutil.which("sshpass") is None, reason="sshpass not installed on test runner"),
]

_IMAGE_TAG = "imbue-mngr-ssh-password-auth-test-sshd:latest"
_USERNAME = "sshuser"
_PASSWORD = "sshpass123"
_RESOURCES_DIR = Path(__file__).parent / "resources"
_TEST_TIMEOUT = 120


@pytest.fixture(scope="session")
def _sshd_image() -> str:
    """Build the test sshd image once per session. Returns the image tag."""
    client = docker.from_env()
    client.images.build(path=str(_RESOURCES_DIR), dockerfile="Dockerfile.test-sshd", tag=_IMAGE_TAG)
    return _IMAGE_TAG


@pytest.fixture
def _sshd_container(_sshd_image: str) -> Generator[tuple[str, int], None, None]:
    """Start an sshd container with password auth. Yields (host, port)."""
    client = docker.from_env()
    container = client.containers.run(_sshd_image, detach=True, ports={"22/tcp": None}, remove=False)
    try:
        container.reload()
        port_mappings = container.ports.get("22/tcp")
        assert port_mappings, f"No port mapping for 22/tcp: {container.ports}"
        port = int(port_mappings[0]["HostPort"])
        wait_for_sshd("127.0.0.1", port, timeout_seconds=30.0)
        yield "127.0.0.1", port
    finally:
        container.remove(force=True)


def _scan_host_key(host: str, port: int, output_path: Path) -> Path:
    """Populate a known_hosts file by running ssh-keyscan."""
    result = subprocess.run(
        ["ssh-keyscan", "-p", str(port), host],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    output_path.write_text(result.stdout)
    return output_path


@pytest.mark.timeout(_TEST_TIMEOUT)
def test_transport_command_authenticates_against_real_sshd(_sshd_container: tuple[str, int], tmp_path: Path) -> None:
    """Assembled sshpass+ssh command authenticates and executes a remote command."""
    host, port = _sshd_container
    known_hosts = _scan_host_key(host, port, tmp_path / "known_hosts")

    auth = SSHPasswordAuth(password=SecretStr(_PASSWORD), known_hosts_file=known_hosts)
    transport = auth.build_transport_command(port=port, known_hosts_file=None)

    # transport.command is the ssh transport string for rsync/git use; it doesn't
    # include a user@host or remote command. Append them to invoke it directly.
    full_cmd = f"{transport.command} {_USERNAME}@{host} 'echo acceptance-ok'"
    env = {**os.environ, **expose_secrets_for_subprocess(transport.env)}
    result = subprocess.run(full_cmd, shell=True, env=env, capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, f"ssh failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "acceptance-ok" in result.stdout


@pytest.mark.timeout(_TEST_TIMEOUT)
def test_transport_command_fails_with_wrong_password(_sshd_container: tuple[str, int], tmp_path: Path) -> None:
    """Wrong password via the assembled command fails authentication."""
    host, port = _sshd_container
    known_hosts = _scan_host_key(host, port, tmp_path / "known_hosts")

    auth = SSHPasswordAuth(password=SecretStr("not-the-right-password"), known_hosts_file=known_hosts)
    transport = auth.build_transport_command(port=port, known_hosts_file=None)

    full_cmd = f"{transport.command} {_USERNAME}@{host} 'echo should-not-run'"
    env = {**os.environ, **expose_secrets_for_subprocess(transport.env)}
    result = subprocess.run(full_cmd, shell=True, env=env, capture_output=True, text=True, timeout=30)

    assert result.returncode != 0
    assert "should-not-run" not in result.stdout
