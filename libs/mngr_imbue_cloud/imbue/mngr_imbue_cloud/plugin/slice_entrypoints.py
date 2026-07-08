from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr_imbue_cloud import hookimpl
from imbue.mngr_imbue_cloud.plugin.backends import SliceVpsDockerProviderBackend
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProviderConfig


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the imbue_cloud_slice provider backend (lima-VM slices on bare-metal boxes)."""
    return (SliceVpsDockerProviderBackend, SliceVpsDockerProviderConfig)
