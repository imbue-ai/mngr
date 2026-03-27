from pathlib import Path
from typing import Final

from imbue.mng import hookimpl
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import ProviderInstanceConfig
from imbue.mng.errors import ConfigStructureError
from imbue.mng.interfaces.provider_backend import ProviderBackendInterface
from imbue.mng.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mng.primitives import ProviderBackendName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.local.config import LocalProviderConfig
from imbue.mng.providers.local.instance import LocalProviderInstance

LOCAL_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("local")


class LocalProviderBackend(ProviderBackendInterface):
    """Backend for creating local provider instances.

    The local provider backend creates provider instances that manage the local
    computer as a host. Multiple instances can be created with different names
    and host_dir settings.
    """

    @staticmethod
    def get_name() -> ProviderBackendName:
        return LOCAL_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents directly on your local machine with no isolation"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return LocalProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return "No build arguments are supported for the local provider."

    @staticmethod
    def get_start_args_help() -> str:
        return "No start arguments are supported for the local provider."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mng_ctx: MngContext,
    ) -> ProviderInstanceInterface:
        """Build a local provider instance."""
        if not isinstance(config, LocalProviderConfig):
            raise ConfigStructureError(f"Expected LocalProviderConfig, got {type(config).__name__}")
        # Get host_dir from typed config, falling back to default
        if config.host_dir is not None:
            host_dir = Path(config.host_dir).expanduser()
        else:
            host_dir = Path(mng_ctx.config.default_host_dir).expanduser()

        host_dir.mkdir(parents=True, exist_ok=True)

        return LocalProviderInstance(
            name=name,
            host_dir=host_dir,
            mng_ctx=mng_ctx,
        )


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the local provider backend."""
    return (LocalProviderBackend, LocalProviderConfig)
