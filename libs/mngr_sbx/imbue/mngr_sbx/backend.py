"""Backend definition + pluggy registration for the Docker Sandboxes provider."""

from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_sbx import hookimpl
from imbue.mngr_sbx.config import SbxProviderConfig
from imbue.mngr_sbx.constants import DEFAULT_HOST_DIR
from imbue.mngr_sbx.constants import SBX_BACKEND_NAME
from imbue.mngr_sbx.instance import SbxProviderInstance


class SbxProviderBackend(ProviderBackendInterface):
    """Backend for creating Docker Sandboxes (sbx) provider instances.

    Each sandbox is launched via ``sbx create`` and bridged into mngr's SSH-based
    host abstraction by installing sshd inside and publishing port 22.

    Availability checks (sbx installed, authenticated) are deferred to first use,
    so the backend can be registered even without sbx present (e.g. in CI).
    """

    @staticmethod
    def get_name() -> ProviderBackendName:
        return SBX_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents in Docker Sandboxes (sbx) with SSH access bridged via 'sbx ports'"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return SbxProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return """\
Supported build arguments for the sbx provider:
  --workspace PATH           Primary host workspace mounted into the sandbox.
                             Defaults to the current working directory.
  --extra-workspace SPEC     Additional workspace mount spec (e.g. /path:ro).
                             Can be specified multiple times.
  --template IMAGE           Override the sbx container image (overrides provider default).

Other arguments are passed through to 'sbx create'.
"""

    @staticmethod
    def get_start_args_help() -> str:
        return "Start args are passed directly to 'sbx create' (e.g. --cpus, --memory)."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        if not isinstance(config, SbxProviderConfig):
            raise MngrError(f"Expected SbxProviderConfig, got {type(config).__name__}")

        host_dir = config.host_dir if config.host_dir is not None else Path(DEFAULT_HOST_DIR)
        return SbxProviderInstance(
            name=name,
            host_dir=host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
        )


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the Docker Sandboxes (sbx) provider backend."""
    return (SbxProviderBackend, SbxProviderConfig)
