from pathlib import Path

from pydantic import Field

from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ProviderBackendName


class LocalProviderConfig(ProviderInstanceConfig):
    """Configuration for the local provider backend."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("local"),
        description="Provider backend (always 'local' for this type)",
    )
    host_dir: Path | None = Field(
        default=None,
        description="Base directory for mngr data (defaults to mngr_ctx.config.default_host_dir)",
    )
