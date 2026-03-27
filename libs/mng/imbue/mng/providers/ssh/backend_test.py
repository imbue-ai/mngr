"""Tests for the SSHProviderBackend."""

from pathlib import Path

from imbue.mng.config.data_types import MngContext
from imbue.mng.primitives import ProviderBackendName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.ssh.backend import SSHProviderBackend
from imbue.mng.providers.ssh.backend import SSH_BACKEND_NAME
from imbue.mng.providers.ssh.config import SSHHostConfig
from imbue.mng.providers.ssh.config import SSHProviderConfig
from imbue.mng.providers.ssh.instance import SSHProviderInstance


def test_backend_name() -> None:
    assert SSHProviderBackend.get_name() == SSH_BACKEND_NAME
    assert SSHProviderBackend.get_name() == ProviderBackendName("ssh")


def test_backend_description() -> None:
    assert "ssh" in SSHProviderBackend.get_description().lower()


def test_backend_build_args_help() -> None:
    help_text = SSHProviderBackend.get_build_args_help()
    assert isinstance(help_text, str)
    assert len(help_text) > 0


def test_backend_start_args_help() -> None:
    help_text = SSHProviderBackend.get_start_args_help()
    assert isinstance(help_text, str)
    assert len(help_text) > 0


def test_backend_get_config_class() -> None:
    assert SSHProviderBackend.get_config_class() is SSHProviderConfig


def test_build_provider_instance_returns_ssh_provider_instance(temp_mng_ctx: MngContext) -> None:
    config = SSHProviderConfig(
        hosts={
            "test-host": SSHHostConfig(
                address="localhost",
                port=22,
            )
        }
    )
    instance = SSHProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mng_ctx=temp_mng_ctx,
    )
    assert isinstance(instance, SSHProviderInstance)


def test_build_provider_instance_with_custom_host_dir(temp_mng_ctx: MngContext) -> None:
    config = SSHProviderConfig(
        host_dir=Path("/custom/path"),
        hosts={
            "test-host": SSHHostConfig(address="localhost"),
        },
    )
    instance = SSHProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mng_ctx=temp_mng_ctx,
    )
    assert isinstance(instance, SSHProviderInstance)
    assert instance.host_dir == Path("/custom/path")


def test_build_provider_instance_uses_default_host_dir(temp_mng_ctx: MngContext) -> None:
    config = SSHProviderConfig(
        hosts={
            "test-host": SSHHostConfig(address="localhost"),
        },
    )
    instance = SSHProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mng_ctx=temp_mng_ctx,
    )
    assert instance.host_dir == Path("/tmp/mng")


def test_build_provider_instance_uses_name(temp_mng_ctx: MngContext) -> None:
    config = SSHProviderConfig(
        hosts={
            "test-host": SSHHostConfig(address="localhost"),
        },
    )
    instance = SSHProviderBackend.build_provider_instance(
        name=ProviderInstanceName("my-ssh"),
        config=config,
        mng_ctx=temp_mng_ctx,
    )
    assert instance.name == ProviderInstanceName("my-ssh")


def test_build_provider_instance_parses_hosts(temp_mng_ctx: MngContext) -> None:
    config = SSHProviderConfig(
        hosts={
            "server1": SSHHostConfig(
                address="192.168.1.1",
                port=2222,
                user="admin",
            ),
            "server2": SSHHostConfig(
                address="192.168.1.2",
            ),
        },
    )
    instance = SSHProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mng_ctx=temp_mng_ctx,
    )
    assert isinstance(instance, SSHProviderInstance)
    assert len(instance.hosts) == 2
    assert "server1" in instance.hosts
    assert "server2" in instance.hosts

    assert instance.hosts["server1"].address == "192.168.1.1"
    assert instance.hosts["server1"].port == 2222
    assert instance.hosts["server1"].user == "admin"

    assert instance.hosts["server2"].address == "192.168.1.2"
    # Verify default values are used
    assert instance.hosts["server2"].port == 22
    assert instance.hosts["server2"].user == "root"


def test_build_provider_instance_with_key_file(tmp_path: Path, temp_mng_ctx: MngContext) -> None:
    key_path = tmp_path / "test.key"
    key_path.write_text("fake-key")

    config = SSHProviderConfig(
        hosts={
            "server1": SSHHostConfig(
                address="localhost",
                key_file=key_path,
            ),
        },
    )
    instance = SSHProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mng_ctx=temp_mng_ctx,
    )
    assert isinstance(instance, SSHProviderInstance)
    assert instance.hosts["server1"].key_file == key_path


def test_ssh_host_config_defaults() -> None:
    config = SSHHostConfig(address="localhost")
    assert config.address == "localhost"
    assert config.port == 22
    assert config.user == "root"
    assert config.key_file is None
