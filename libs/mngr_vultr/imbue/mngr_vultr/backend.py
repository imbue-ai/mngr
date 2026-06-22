from collections.abc import Sequence
from typing import Any
from typing import Final

from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderNotAuthorizedError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vps.build_args import ParsedVpsBuildOptions
from imbue.mngr_vps.build_args import parse_vps_build_args
from imbue.mngr_vps.instance import VpsProvider
from imbue.mngr_vultr import hookimpl
from imbue.mngr_vultr.client import VultrVpsClient
from imbue.mngr_vultr.config import VultrProviderConfig

VULTR_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("vultr")


class VultrProvider(VpsProvider):
    """Vultr-specific provider that implements VPS listing via the Vultr API.

    All cross-VPS discovery machinery (parallel SSH reads, caching,
    per-name lookups) is inherited from ``VpsProvider``; this
    subclass only contributes the provider-specific tag-based listing.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    vultr_client: VultrVpsClient = Field(frozen=True, description="Vultr API client")
    vultr_config: VultrProviderConfig = Field(frozen=True, description="Vultr-specific configuration")

    def _fetch_provider_instances(self) -> list[dict[str, Any]]:
        """List every Vultr instance in the account.

        Vultr's API has a ``tag`` filter but Vultr stores tags as flat
        ``"key=value"`` strings, so an exact-match filter doesn't compose with
        our ``mngr-provider=<name>`` convention the same way AWS's
        ``tag:mngr-provider`` filter does. List everything and let the shared
        discovery flow filter by SSH-reachability + state-container presence.
        """
        return self.vultr_client.list_instances()

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        """Parse Vultr-prefixed build args (--vultr-region, --vultr-plan, --git-depth)."""
        return parse_vps_build_args(
            build_args,
            provider_prefix="vultr",
            default_region=self.vultr_config.default_region,
            default_plan=self.vultr_config.default_plan,
            plan_arg_name="plan",
        )

    def _list_provider_vps_hostnames(self) -> list[str]:
        """Return public IPs of Vultr instances tagged with this provider's name.

        Vultr uses raw IPv4 addresses as SSH targets, not DNS names. The
        return values are strings to satisfy the base-class contract,
        which accepts either IPs or hostnames. The API key is guaranteed present
        (the backend raises ProviderNotAuthorizedError at construction otherwise).
        """
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
            "Vultr-specific args (consumed by provider, not passed to docker):\n"
            "  --vultr-region=REGION  Vultr region (default: ewr)\n"
            "  --vultr-plan=PLAN      Vultr plan (default: vc2-2c-4gb)\n"
            "  --git-depth=N          Shallow-clone build context to depth N before upload\n"
            "\n"
            "All other build args are passed to 'docker build' on the VPS.\n"
            "Example: -b --vultr-plan=vc2-2c-4gb -b --file=Dockerfile -b .\n"
        )

    @staticmethod
    def get_start_args_help() -> str:
        return "Start args are passed directly to 'docker run'. Run 'docker run --help' for details."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        if not isinstance(config, VultrProviderConfig):
            raise MngrError(f"Expected VultrProviderConfig, got {type(config).__name__}")

        # An enabled-but-unauthenticated provider is an error, not a silent
        # zero-result listing: surface it consistently with the other cloud
        # providers so it is reported (and contributes a non-zero exit) rather
        # than vanishing. ProviderNotAuthorizedError is a ProviderUnavailableError,
        # so read paths still treat it as unavailable.
        try:
            api_key = config.get_api_key()
        except ValueError as e:
            raise ProviderNotAuthorizedError(
                name,
                reason="Vultr API key not configured",
                short_remediation="set the Vultr API key (VULTR_API_KEY or providers.<name>.api_key)",
            ) from e
        vultr_client = VultrVpsClient(api_key=SecretStr(api_key), os_id=config.default_os_id)

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
