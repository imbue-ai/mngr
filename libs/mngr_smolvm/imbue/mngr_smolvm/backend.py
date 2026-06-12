from pathlib import Path

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_smolvm import hookimpl
from imbue.mngr_smolvm.config import SmolvmProviderConfig
from imbue.mngr_smolvm.constants import DEFAULT_HOST_DIR
from imbue.mngr_smolvm.constants import SMOLVM_BACKEND_NAME
from imbue.mngr_smolvm.instance import SmolvmProviderInstance


class SmolvmProviderBackend(ProviderBackendInterface):
    """Backend for creating smolvm microVM provider instances.

    The smolvm provider backend creates provider instances that manage
    smolvm machines (libkrun microVMs: KVM on Linux, Hypervisor.framework
    on macOS) as hosts. mngr provisions sshd inside each machine via the
    smolvm exec channel and then accesses it over SSH on a forwarded
    localhost port.

    smolvm installation and version checks are deferred to first use (not
    checked at construction time) so that the provider can be registered
    without smolvm being installed.
    """

    @staticmethod
    def get_name() -> ProviderBackendName:
        return SMOLVM_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents in smolvm microVMs (libkrun/KVM) with SSH access"

    @staticmethod
    def get_build_args_help() -> str:
        return """\
Supported build arguments for the smolvm provider:
  --image-archive PATH  Create the host from a local image archive (the output
                        of 'docker save'). The archive is converted to a
                        .smolmachine pack (cached by content hash) and the
                        machine is created from it.
  --from PATH           Create the host from an existing .smolmachine pack.
When neither is given, the host runs the --image OCI reference (pulled from a
registry), or a bare Alpine VM when no image is specified at all.
"""

    @staticmethod
    def get_start_args_help() -> str:
        return """\
Start args are passed through to 'smolvm machine create'. Common options:
  --cpus N              Number of vCPUs (default: 4)
  --mem MiB             Memory in MiB (default: 4096)
  --storage GiB         OCI layer storage disk size (default: 20)
  --overlay GiB         Persistent rootfs overlay disk size (default: 10)
  --allow-cidr CIDR     Restrict egress to the given CIDR (repeatable)
  --allow-host HOST     Restrict egress to the given hostname (repeatable)
Run 'smolvm machine create --help' for the full list.
"""

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return SmolvmProviderConfig

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        """Build a smolvm provider instance.

        smolvm installation and version checks are deferred to first use,
        not performed here. This allows the provider to be registered in
        environments where smolvm is not installed (e.g. CI).
        """
        if not isinstance(config, SmolvmProviderConfig):
            raise MngrError(f"Expected SmolvmProviderConfig, got {type(config).__name__}")

        host_dir = config.host_dir if config.host_dir is not None else Path(DEFAULT_HOST_DIR)
        return SmolvmProviderInstance(
            name=name,
            host_dir=host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
        )


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the smolvm provider backend."""
    return (SmolvmProviderBackend, SmolvmProviderConfig)
