"""Integration tests for the SSH provider using a local sshd instance.

These tests require openssh-server to be installed on the system.
They start a local sshd instance on a random port for testing.
"""

import os
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.mng.config.data_types import MngContext
from imbue.mng.primitives import HostName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.ssh.instance import SSHHostConfig
from imbue.mng.providers.ssh.instance import SSHProviderInstance
from imbue.mng.testing.testing import generate_ssh_keypair
from imbue.mng.testing.testing import local_sshd


@pytest.fixture
def ssh_provider(
    tmp_path: Path,
    temp_host_dir: Path,
    temp_mng_ctx: MngContext,
) -> Generator[SSHProviderInstance, None, None]:
    """Fixture that provides an SSHProviderInstance connected to a local sshd."""
    private_key, public_key = generate_ssh_keypair(tmp_path)
    public_key_content = public_key.read_text()

    with local_sshd(public_key_content, tmp_path) as (port, _host_key):
        current_user = os.environ.get("USER", "root")
        provider = SSHProviderInstance(
            name=ProviderInstanceName("ssh-test"),
            host_dir=temp_host_dir,
            mng_ctx=temp_mng_ctx,
            hosts={
                "localhost": SSHHostConfig(
                    address="127.0.0.1",
                    port=port,
                    user=current_user,
                    key_file=private_key,
                ),
            },
        )

        yield provider


@pytest.mark.acceptance
@pytest.mark.timeout(60)
def test_ssh_provider_get_host(ssh_provider: SSHProviderInstance) -> None:
    """Test getting a host by name from SSH provider."""
    host = ssh_provider.get_host(HostName("localhost"))
    assert host is not None
    assert host.id is not None


@pytest.mark.acceptance
@pytest.mark.timeout(60)
def test_ssh_provider_get_host_by_id(ssh_provider: SSHProviderInstance) -> None:
    """Test getting a host by ID from SSH provider."""
    host_by_name = ssh_provider.get_host(HostName("localhost"))
    host_by_id = ssh_provider.get_host(host_by_name.id)
    assert host_by_id.id == host_by_name.id


@pytest.mark.acceptance
@pytest.mark.timeout(60)
def test_ssh_provider_list_hosts(ssh_provider: SSHProviderInstance) -> None:
    """Test listing hosts from SSH provider."""
    hosts = ssh_provider.list_hosts(cg=ssh_provider.mng_ctx.concurrency_group)
    assert len(hosts) == 1
    assert hosts[0].id == ssh_provider.get_host(HostName("localhost")).id


@pytest.mark.acceptance
@pytest.mark.timeout(60)
def test_ssh_provider_execute_command(ssh_provider: SSHProviderInstance) -> None:
    """Test executing a command on an SSH host."""
    host = ssh_provider.get_host(HostName("localhost"))
    result = host.execute_command("echo hello")
    assert result.success
    assert "hello" in result.stdout


@pytest.mark.acceptance
@pytest.mark.timeout(60)
def test_ssh_provider_host_id_is_deterministic(ssh_provider: SSHProviderInstance) -> None:
    """Test that the same host name always produces the same host ID."""
    host1 = ssh_provider.get_host(HostName("localhost"))
    host2 = ssh_provider.get_host(HostName("localhost"))
    assert host1.id == host2.id


@pytest.mark.acceptance
@pytest.mark.timeout(60)
def test_ssh_provider_create_host_raises_not_implemented(ssh_provider: SSHProviderInstance) -> None:
    """Test that create_host raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        ssh_provider.create_host(HostName("localhost"))


@pytest.mark.acceptance
@pytest.mark.timeout(60)
def test_ssh_provider_destroy_host_raises_not_implemented(ssh_provider: SSHProviderInstance) -> None:
    """Test that destroy_host raises NotImplementedError."""
    host = ssh_provider.get_host(HostName("localhost"))
    with pytest.raises(NotImplementedError):
        ssh_provider.destroy_host(host)
