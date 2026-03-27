from __future__ import annotations

from pathlib import Path
from typing import Final

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import ConfigStructureError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.ssh.config import SSHHostConfig
from imbue.mngr.providers.ssh.config import SSHProviderConfig
from imbue.mngr.providers.ssh.instance import SSHProviderInstance

SSH_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("ssh")


class SSHProviderBackend(ProviderBackendInterface):
    """Backend for creating SSH provider instances.

    The SSH provider connects to pre-configured hosts via SSH. Unlike cloud
    providers, it does not create or destroy hosts - they must already exist.

    This provider does not support:
    - Tags (hosts are statically configured)
    - Snapshots (no cloud infrastructure)
    - Creating/destroying hosts (they're pre-existing)
    """

    @staticmethod
    def get_name() -> ProviderBackendName:
        return SSH_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Connects to pre-configured hosts via SSH (static host pool)"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return SSHProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return """\
The SSH provider does not support creating hosts dynamically.
Hosts must be pre-configured in the mngr config file.

Example configuration in mngr.toml:
  [providers.my-ssh-pool]
  backend = "ssh"

  [providers.my-ssh-pool.hosts.server1]
  address = "192.168.1.100"
  port = 22
  user = "root"
  key_file = "~/.ssh/id_ed25519"
"""

    @staticmethod
    def get_start_args_help() -> str:
        return "No start arguments are supported for the SSH provider."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        """Build an SSH provider instance."""
        if not isinstance(config, SSHProviderConfig):
            raise ConfigStructureError(f"Expected SSHProviderConfig, got {type(config).__name__}")
        host_dir = config.host_dir
        hosts = config.hosts
        # Expand key_file paths
        expanded_hosts: dict[str, SSHHostConfig] = {}
        for host_name, host_config in hosts.items():
            if host_config.key_file is not None:
                expanded_hosts[host_name] = SSHHostConfig(
                    address=host_config.address,
                    port=host_config.port,
                    user=host_config.user,
                    key_file=Path(host_config.key_file).expanduser(),
                )
            else:
                expanded_hosts[host_name] = host_config
        hosts = expanded_hosts

        return SSHProviderInstance(
            name=name,
            host_dir=host_dir,
            mngr_ctx=mngr_ctx,
            hosts=hosts,
        )


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the SSH provider backend."""
    return (SSHProviderBackend, SSHProviderConfig)
