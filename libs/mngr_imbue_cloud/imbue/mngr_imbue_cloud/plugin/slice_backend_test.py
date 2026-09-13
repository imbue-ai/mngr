from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.plugin.backends import SliceVpsDockerProviderBackend
from imbue.mngr_imbue_cloud.plugin.slice_entrypoints import register_provider_backend
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProvider
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProviderConfig


def test_backend_name_and_config_class() -> None:
    assert SliceVpsDockerProviderBackend.get_name() == ProviderBackendName("imbue_cloud_slice")
    assert SliceVpsDockerProviderBackend.get_config_class() is SliceVpsDockerProviderConfig


def test_plugin_registers_the_slice_backend() -> None:
    backend, config_class = register_provider_backend()
    assert backend is SliceVpsDockerProviderBackend
    assert config_class is SliceVpsDockerProviderConfig


def test_build_provider_instance_wires_lima_client_and_slice_config(temp_mngr_ctx: MngrContext) -> None:
    config = SliceVpsDockerProviderConfig(
        backend=ProviderBackendName("imbue_cloud_slice"),
        box_public_address="15.204.140.221",
        slice_vcpus=2,
    )
    provider = SliceVpsDockerProviderBackend.build_provider_instance(
        ProviderInstanceName("imbue_cloud_slice"), config, temp_mngr_ctx
    )
    assert isinstance(provider, SliceVpsDockerProvider)
    # The base vps_client and the narrow lima_client are the same object, and the
    # slice config is exposed for the slice-specific knobs.
    assert provider.lima_client is provider.vps_client
    assert provider.slice_config.box_public_address == "15.204.140.221"
