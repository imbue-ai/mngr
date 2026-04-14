from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_lima import hookimpl
from imbue.mngr_lima.config import LimaProviderConfig
from imbue.mngr_lima.constants import DEFAULT_HOST_DIR
from imbue.mngr_lima.constants import LIMA_BACKEND_NAME
from imbue.mngr_lima.instance import LimaProviderInstance


class LimaProviderBackend(ProviderBackendInterface):
    """Backend for creating Lima VM provider instances.

    The Lima provider backend creates provider instances that manage Lima VMs
    as hosts. Each VM is accessed via SSH using Lima's built-in SSH management.

    Lima installation and version checks are deferred to first use (not
    checked at construction time) so that the provider can be registered
    without limactl being installed. This matches how the Docker provider
    lazily creates its Docker client.
    """

    @staticmethod
    def get_name() -> ProviderBackendName:
        return LIMA_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents in Lima VMs (QEMU/VZ) with SSH access"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return LimaProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return """\
Supported build arguments for the lima provider:
  --file PATH           Path to a Lima YAML config file for full VM customization.
                        When not specified, a default config is generated with the
                        mngr pre-built image.
"""

    @staticmethod
    def get_start_args_help() -> str:
        return """\
Start args are passed directly to 'limactl start'. Common options:
  --cpus=N              Number of CPU cores (default: 4)
  --memory=NGiB         Memory size (default: 4GiB)
  --disk=NGiB           Disk size (default: 100GiB)
  --vm-type=TYPE        VM type: qemu or vz (default: auto-detected)
  --mount-writable      Make default mounts writable
Run 'limactl start --help' for the full list.
"""

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        """Build a Lima provider instance.

        Lima installation and version checks are deferred to first use,
        not performed here. This allows the provider to be registered in
        environments where limactl is not installed (e.g. CI).
        """
        if not isinstance(config, LimaProviderConfig):
            raise MngrError(f"Expected LimaProviderConfig, got {type(config).__name__}")

        host_dir = config.host_dir if config.host_dir is not None else Path(DEFAULT_HOST_DIR)
        return LimaProviderInstance(
            name=name,
            host_dir=host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
        )


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the Lima provider backend."""
    return (LimaProviderBackend, LimaProviderConfig)
