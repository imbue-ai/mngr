"""Tests for the LocalProviderBackend."""

import os
from pathlib import Path

import pluggy

from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.backend import LOCAL_BACKEND_NAME
from imbue.mngr.providers.local.backend import LocalProviderBackend
from imbue.mngr.providers.local.config import LocalProviderConfig
from imbue.mngr.providers.local.instance import LocalProviderInstance


def test_backend_name() -> None:
    assert LocalProviderBackend.get_name() == LOCAL_BACKEND_NAME
    assert LocalProviderBackend.get_name() == ProviderBackendName("local")


def test_backend_description() -> None:
    assert "local" in LocalProviderBackend.get_description().lower()


def test_backend_build_args_help() -> None:
    help_text = LocalProviderBackend.get_build_args_help()
    assert isinstance(help_text, str)
    assert len(help_text) > 0


def test_backend_start_args_help() -> None:
    help_text = LocalProviderBackend.get_start_args_help()
    assert isinstance(help_text, str)
    assert len(help_text) > 0


def test_backend_get_config_class() -> None:
    assert LocalProviderBackend.get_config_class() is LocalProviderConfig


def test_build_provider_instance_returns_local_provider_instance(temp_mngr_ctx: MngrContext) -> None:
    config = LocalProviderConfig()
    instance = LocalProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    assert isinstance(instance, LocalProviderInstance)


def test_build_provider_instance_with_custom_host_dir(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    custom_dir = tmp_path / "custom_host_dir"
    custom_dir.mkdir()
    config = LocalProviderConfig(host_dir=custom_dir)
    instance = LocalProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    assert isinstance(instance, LocalProviderInstance)
    # host_dir should be the custom_dir directly
    assert instance.host_dir == custom_dir


def test_build_provider_instance_uses_default_host_dir(temp_mngr_ctx: MngrContext) -> None:
    config = LocalProviderConfig()
    instance = LocalProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    # host_dir should be the expanded default_host_dir directly
    expanded_default = Path(os.path.expanduser(str(temp_mngr_ctx.config.default_host_dir)))
    assert instance.host_dir == expanded_default


def test_build_provider_instance_uses_config_default_host_dir(temp_mngr_ctx: MngrContext) -> None:
    config = LocalProviderConfig()
    instance = LocalProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    # host_dir should equal the expanded default_host_dir
    assert instance.host_dir == temp_mngr_ctx.config.default_host_dir.expanduser()


def test_build_provider_instance_uses_name(temp_mngr_ctx: MngrContext) -> None:
    config = LocalProviderConfig()
    instance = LocalProviderBackend.build_provider_instance(
        name=ProviderInstanceName("my-local"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )
    assert instance.name == ProviderInstanceName("my-local")


def test_built_instance_can_create_host(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    custom_dir = tmp_path / "host_dir"
    custom_dir.mkdir()
    config = LocalProviderConfig(host_dir=custom_dir)
    instance = LocalProviderBackend.build_provider_instance(
        name=ProviderInstanceName("test"),
        config=config,
        mngr_ctx=temp_mngr_ctx,
    )

    host = instance.create_host(HostName("localhost"))
    assert host is not None
    assert host.id is not None


def test_multiple_instances_with_different_names(
    tmp_path: Path,
    temp_profile_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    # Create two separate host directories
    tmpdir1 = tmp_path / "host1"
    tmpdir2 = tmp_path / "host2"
    tmpdir1.mkdir()
    tmpdir2.mkdir()

    mngr_ctx1 = MngrContext(
        config=MngrConfig(default_host_dir=tmpdir1, prefix=mngr_test_prefix),
        pm=plugin_manager,
        profile_dir=temp_profile_dir,
    )
    mngr_ctx2 = MngrContext(
        config=MngrConfig(default_host_dir=tmpdir2, prefix=mngr_test_prefix),
        pm=plugin_manager,
        profile_dir=temp_profile_dir,
    )
    config1 = LocalProviderConfig(host_dir=tmpdir1)
    config2 = LocalProviderConfig(host_dir=tmpdir2)
    instance1 = LocalProviderBackend.build_provider_instance(
        name=ProviderInstanceName("local-1"),
        config=config1,
        mngr_ctx=mngr_ctx1,
    )
    instance2 = LocalProviderBackend.build_provider_instance(
        name=ProviderInstanceName("local-2"),
        config=config2,
        mngr_ctx=mngr_ctx2,
    )

    assert instance1.name == ProviderInstanceName("local-1")
    assert instance2.name == ProviderInstanceName("local-2")

    host1 = instance1.create_host(HostName("localhost"))
    host2 = instance2.create_host(HostName("localhost"))

    assert host1.id != host2.id
