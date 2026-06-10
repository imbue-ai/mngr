from pathlib import Path

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import ProviderBackendName


class SSHHostConfig(FrozenModel):
    """Configuration for a single SSH host in the pool."""

    address: str = Field(description="SSH hostname or IP address")
    port: int = Field(default=22, description="SSH port number")
    user: str = Field(default="root", description="SSH username")
    key_file: Path | None = Field(default=None, description="Path to SSH private key file")
    known_hosts_file: Path | None = Field(
        default=None, description="Path to known_hosts file for host key verification"
    )

    def with_expanded_key_file(self) -> "SSHHostConfig":
        """Return a copy with key_file expanded (~ resolved), or self when no key_file is set."""
        if self.key_file is None:
            return self
        # Update only key_file so every other field (notably known_hosts_file) is preserved.
        return self.model_copy_update(
            to_update(self.field_ref().key_file, self.key_file.expanduser()),
        )


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
    dynamic_hosts_file: Path | None = Field(
        default=None,
        description="Path to a TOML file with dynamically registered hosts. Defaults to <profile_dir>/providers/<instance-name>/dynamic_hosts.toml",
    )
