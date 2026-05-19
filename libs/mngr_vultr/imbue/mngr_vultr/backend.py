from typing import Any
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vps_docker.instance import VpsDockerProvider
from imbue.mngr_vultr import hookimpl
from imbue.mngr_vultr.client import VultrVpsClient
from imbue.mngr_vultr.config import VultrProviderConfig

VULTR_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("vultr")


class VultrProvider(VpsDockerProvider):
    """Vultr-specific provider that implements VPS listing via the Vultr API.

    All cross-VPS discovery machinery (parallel SSH reads, caching,
    per-name lookups) is inherited from ``VpsDockerProvider``; this
    subclass only contributes the provider-specific tag-based listing.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    vultr_client: VultrVpsClient = Field(frozen=True, description="Vultr API client")
    vultr_config: VultrProviderConfig = Field(frozen=True, description="Vultr-specific configuration")

    _instances_cache: list[dict[str, Any]] | None = PrivateAttr(default=None)

    def reset_caches(self) -> None:
        super().reset_caches()
        self._instances_cache = None

    def _list_instances_cached(self) -> list[dict[str, Any]]:
        """List Vultr instances, caching the result for the duration of the command."""
        if self._instances_cache is not None:
            return self._instances_cache
        self._instances_cache = self.vultr_client.list_instances()
        return self._instances_cache

    def _list_provider_vps_hostnames(self) -> list[str]:
        """Return public IPs of Vultr instances tagged with this provider's name.

        Vultr uses raw IPv4 addresses as SSH targets, not DNS names. The
        return values are strings to satisfy the base-class contract,
        which accepts either IPs or hostnames.
        """
        if not self.vultr_client.api_key.get_secret_value():
            logger.warning("Vultr API key not configured, skipping VPS discovery")
            return []
        provider_tag = f"mngr-provider={self.name}"
        instances = self._list_instances_cached()
        vps_ips: list[str] = []
        for instance in instances:
            if provider_tag not in instance.get("tags", []):
                continue
            vps_ip = instance.get("main_ip", "")
            if vps_ip and vps_ip != "0.0.0.0":
                vps_ips.append(vps_ip)
        return vps_ips


class VultrProviderBackend(ProviderBackendInterface):
    """Backend for creating Vultr VPS Docker provider instances."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return VULTR_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents in Docker containers on Vultr VPS instances"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return VultrProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return (
            "VPS-specific args (consumed by provider, not passed to docker):\n"
            "  --vps-region=REGION  Vultr region (default: ewr)\n"
            "  --vps-plan=PLAN      Vultr plan (default: vc2-2c-4gb)\n"
            "  --vps-os=OS_ID       Vultr OS ID (default: 2136 = Debian 12 x64)\n"
            "  --git-depth=N        Shallow-clone build context to depth N before upload\n"
            "\n"
            "All other build args are passed to 'docker build' on the VPS.\n"
            "Example: -b --vps-plan=vc2-2c-4gb -b --file=Dockerfile -b .\n"
        )

    @staticmethod
    def get_start_args_help() -> str:
        return "Start args are passed directly to 'docker run'. Run 'docker run --help' for details."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
        is_for_host_creation: bool = False,
    ) -> ProviderInstanceInterface:
        """Build a Vultr provider instance.

        ``is_for_host_creation`` is ignored: the Vultr backend has no one-time
        bootstrap resources to gate on (compare the Modal backend, which uses
        this flag to authorize creating a missing per-user env).
        """
        del is_for_host_creation
        if not isinstance(config, VultrProviderConfig):
            raise MngrError(f"Expected VultrProviderConfig, got {type(config).__name__}")

        try:
            api_key = config.get_api_key()
        except ValueError:
            # No API key configured -- create with empty key.
            # The provider will be discoverable but discovery operations will
            # return empty results and log a warning when the API is called.
            api_key = ""
        vultr_client = VultrVpsClient(api_key=SecretStr(api_key))

        return VultrProvider(
            name=name,
            host_dir=config.host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
            vps_client=vultr_client,
            vultr_client=vultr_client,
            vultr_config=config,
        )


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the Vultr provider backend."""
    return (VultrProviderBackend, VultrProviderConfig)
