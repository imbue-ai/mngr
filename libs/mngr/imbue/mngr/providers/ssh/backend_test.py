"""Tests for the SSHProviderBackend."""

from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.loader import parse_config
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.ssh.backend import SSHProviderBackend
from imbue.mngr.providers.ssh.backend import SSH_BACKEND_NAME
from imbue.mngr.providers.ssh.config import SSHHostConfig
from imbue.mngr.providers.ssh.config import SSHProviderConfig
from imbue.mngr.providers.ssh.instance import SSHProviderInstance


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


def test_build_provider_instance_returns_ssh_provider_instance(temp_mngr_ctx: MngrContext) -> None:
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
        mngr_ctx=temp_mngr_ctx,
    )
    assert isinstance(instance, SSHProviderInstance)


def test_build_provider_instance_with_custom_host_dir(temp_mngr_ctx: MngrContext) -> None:
    config = SSHProviderConfig(
        host_dir=Path("/custom/path"),
        hosts={
            "test-host": SSHHostConfig(address="localhost"),
        },
    )
    instance = SSHProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    assert isinstance(instance, SSHProviderInstance)
    assert instance.host_dir == Path("/custom/path")


def test_build_provider_instance_uses_default_host_dir(temp_mngr_ctx: MngrContext) -> None:
    config = SSHProviderConfig(
        hosts={
            "test-host": SSHHostConfig(address="localhost"),
        },
    )
    instance = SSHProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    assert instance.host_dir == Path("/tmp/mngr")


def test_build_provider_instance_uses_name(temp_mngr_ctx: MngrContext) -> None:
    config = SSHProviderConfig(
        hosts={
            "test-host": SSHHostConfig(address="localhost"),
        },
    )
    instance = SSHProviderBackend.build_provider_instance(
        name=ProviderInstanceName("my-ssh"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    assert instance.name == ProviderInstanceName("my-ssh")


def test_build_provider_instance_parses_hosts(temp_mngr_ctx: MngrContext) -> None:
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
        mngr_ctx=temp_mngr_ctx,
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


def test_build_provider_instance_with_key_file(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
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
        mngr_ctx=temp_mngr_ctx,
    )
    assert isinstance(instance, SSHProviderInstance)
    assert instance.hosts["server1"].key_file == key_path


def test_build_provider_instance_preserves_known_hosts_file_with_key_file(temp_mngr_ctx: MngrContext) -> None:
    """When a host sets both ``key_file`` and ``known_hosts_file``, expanding the
    ``key_file`` path must not drop ``known_hosts_file``.

    Regression test: ``build_provider_instance`` rebuilt ``SSHHostConfig`` while
    expanding ``key_file`` but omitted ``known_hosts_file``, silently disabling
    strict host-key checking for any host that configured both. The dynamic-hosts
    path in ``SSHProviderInstance._read_dynamic_hosts`` already preserves it.
    """
    known_hosts_path = Path("/etc/ssh/ssh_known_hosts")
    config = SSHProviderConfig(
        hosts={
            "server1": SSHHostConfig(
                address="localhost",
                key_file=Path("~/.ssh/id_ed25519"),
                known_hosts_file=known_hosts_path,
            ),
        },
    )
    instance = SSHProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    assert isinstance(instance, SSHProviderInstance)
    host_config = instance.hosts["server1"]
    # known_hosts_file must be preserved exactly (strict host-key checking stays on).
    assert host_config.known_hosts_file == known_hosts_path
    # key_file must still be expanded (no leading "~").
    assert host_config.key_file == Path("~/.ssh/id_ed25519").expanduser()
    assert not str(host_config.key_file).startswith("~")


def test_ssh_host_config_defaults() -> None:
    config = SSHHostConfig(address="localhost")
    assert config.address == "localhost"
    assert config.port == 22
    assert config.user == "root"
    assert config.key_file is None


def test_static_hosts_from_settings_dict_are_coerced(temp_mngr_ctx: MngrContext) -> None:
    """Static ``[providers.*.hosts.*]`` tables loaded through the config parser
    must become ``SSHHostConfig`` objects, not raw dicts.

    Regression test: provider configs are built with ``model_construct`` (to keep
    unset top-level fields ``None`` for config-layer merging), which skips coercion
    of nested model fields. Without coercion, ``hosts`` entries stayed raw dicts and
    every host-enumerating command (``mngr list``, ``mngr connect``, ...) crashed
    with ``AttributeError: 'dict' object has no attribute 'key_file'`` while
    building the provider instance. This exercises the real parse path
    (``parse_config``), unlike the other tests here which hand in already-built
    ``SSHHostConfig`` objects.
    """
    raw_settings = {
        "providers": {
            "my-ssh": {
                "backend": "ssh",
                "hosts": {
                    "server1": {"address": "192.168.1.1", "key_file": "~/.ssh/id_rsa"},
                },
            },
        },
    }
    config = parse_config(raw_settings, disabled_plugins=frozenset())
    provider_config = config.providers[ProviderInstanceName("my-ssh")]
    assert isinstance(provider_config, SSHProviderConfig)
    assert isinstance(provider_config.hosts["server1"], SSHHostConfig)

    # End-to-end: building the instance and resolving the host must not raise.
    instance = SSHProviderBackend.build_provider_instance(
        name=ProviderInstanceName("my-ssh"),
        config=provider_config,
        mngr_ctx=temp_mngr_ctx,
    )
    host = instance.get_host(HostName("server1"))
    assert host.id is not None
