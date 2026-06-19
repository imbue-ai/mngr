import os

from pydantic import Field
from pydantic import SecretStr

from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr_vps.config import VpsProviderConfig


class VultrConfigError(MngrError, ValueError):
    """Raised when the Vultr provider configuration is incomplete or invalid."""


class VultrProviderConfig(VpsProviderConfig):
    """Configuration for the Vultr VPS Docker provider."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("vultr"),
        description="Provider backend (always 'vultr' for this type)",
    )
    api_key: SecretStr | None = Field(
        default=None,
        description="Vultr API key. Falls back to VULTR_API_KEY env var.",
    )
    default_region: str = Field(
        default="ewr",
        description="Default Vultr region",
    )
    default_plan: str = Field(
        default="vc2-2c-4gb",
        description="Default Vultr plan",
    )
    default_os_id: int = Field(
        default=2136,
        description="Default Vultr OS ID (Debian 12 x64)",
    )

    def get_api_key(self) -> str:
        """Resolve the API key from config or environment."""
        if self.api_key is not None:
            return self.api_key.get_secret_value()
        env_key = os.environ.get("VULTR_API_KEY")
        if env_key is not None:
            return env_key
        raise VultrConfigError(
            "Vultr API key not configured. Set VULTR_API_KEY environment variable "
            "or add api_key to the provider config."
        )
