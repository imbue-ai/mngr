from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ProviderBackendName


class SSHHostConfig(FrozenModel):
    """Configuration for a single SSH host in the pool."""

    address: str = Field(description="SSH hostname or IP address")
    port: int = Field(default=22, description="SSH port number")
    user: str = Field(default="root", description="SSH username")
    key_file: Path | None = Field(default=None, description="Path to SSH private key file")


class SSHProviderConfig(ProviderInstanceConfig):
    """Configuration for the SSH provider backend."""

    backend: ProviderBackendName = Field(
        default=ProviderBackendName("ssh"),
        description="Provider backend (always 'ssh' for this type)",
    )
    host_dir: Path = Field(
        default=Path("/tmp/mngr"),
        description="Directory for mngr state on remote hosts",
    )
    hosts: dict[str, SSHHostConfig] = Field(
        default_factory=dict,
        description="Map of host name to SSH configuration",
    )
