from pathlib import Path

from pydantic import Field

from imbue.mng.config.data_types import ProviderInstanceConfig
from imbue.mng.primitives import ProviderBackendName


class LocalProviderConfig(ProviderInstanceConfig):
    """Configuration for the local provider backend."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("local"),
        description="Provider backend (always 'local' for this type)",
    )
    host_dir: Path | None = Field(
        default=None,
        description="Base directory for mng data (defaults to mng_ctx.config.default_host_dir)",
    )
