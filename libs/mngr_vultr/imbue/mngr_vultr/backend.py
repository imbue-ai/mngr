from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import DiscoveredHost
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vultr import hookimpl
from imbue.mngr_vultr.client import VultrVpsClient
from imbue.mngr_vultr.config import VultrProviderConfig
from imbue.mngr_vps_docker.errors import VpsConnectionError
from imbue.mngr_vps_docker.errors import ContainerSetupError
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.instance import VpsDockerProvider

VULTR_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("vultr")


class VultrProvider(VpsDockerProvider):
    """Vultr-specific provider that overrides discovery to use the Vultr API."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    vultr_client: VultrVpsClient = Field(frozen=True, description="Vultr API client")
    vultr_config: VultrProviderConfig = Field(frozen=True, description="Vultr-specific configuration")

    def _discover_host_records(self) -> list[VpsDockerHostRecord]:
        """Discover host records by querying the Vultr API for tagged instances."""
        all_records: list[VpsDockerHostRecord] = []

        # Skip discovery if no API key is configured
        if not self.vultr_client.api_key.get_secret_value():
            return []

        # List all Vultr instances and filter for ones tagged with our provider name
        provider_tag = f"mngr-provider={self.name}"
        instances = self.vultr_client.list_instances()

        for instance in instances:
            instance_tags = instance.get("tags", [])
            if provider_tag not in instance_tags:
                continue

            vps_ip = instance.get("main_ip", "")
            if not vps_ip or vps_ip == "0.0.0.0":
                continue

            # SSH to the VPS and read host records from the state volume
            try:
                docker_ssh = self._make_docker_ssh(vps_ip)
                host_store = self._get_host_store(docker_ssh)
                records = host_store.list_all_host_records()
                all_records.extend(records)
            except (VpsConnectionError, ContainerSetupError) as e:
                logger.warning("Failed to read host records from VPS {}: {}", vps_ip, e)
                continue

        return all_records

    def _find_host_record(self, host: HostId | HostName) -> VpsDockerHostRecord | None:
        """Find a host record by ID or name across all known VPSes."""
        if not self.vultr_client.api_key.get_secret_value():
            return None
        records = self._discover_host_records()
        for record in records:
            if isinstance(host, HostId) and record.certified_host_data.host_id == str(host):
                return record
            elif isinstance(host, HostName) and record.certified_host_data.host_name == str(host):
                return record
        return None

    def discover_hosts(
        self,
        cg: ConcurrencyGroup,
        include_destroyed: bool = False,
    ) -> list[DiscoveredHost]:
        """Discover all hosts managed by this Vultr provider."""
        discovered: list[DiscoveredHost] = []

        try:
            all_records = self._discover_host_records()
        except Exception as e:
            logger.warning("Failed to discover Vultr hosts: {}", e)
            return []

        for record in all_records:
            host_id = HostId(record.certified_host_data.host_id)
            host_name = HostName(record.certified_host_data.host_name)
            discovered.append(
                DiscoveredHost(
                    host_id=host_id,
                    host_name=host_name,
                    provider_name=self.name,
                )
            )
            # Cache the host object
            if record.vps_ip is not None and record.config is not None:
                docker_ssh = self._make_docker_ssh(record.vps_ip)
                try:
                    if docker_ssh.container_is_running(record.config.container_name):
                        self._create_host_object(host_id, record.vps_ip, docker_ssh)
                    else:
                        self._create_offline_host(record)
                except (VpsConnectionError, ContainerSetupError):
                    self._create_offline_host(record)
            else:
                self._create_offline_host(record)

        return discovered


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
            "VPS-specific args (--vps- prefix, consumed by provider):\n"
            "  --vps-region=REGION  Vultr region (default: ewr)\n"
            "  --vps-plan=PLAN      Vultr plan (default: vc2-1c-1gb)\n"
            "  --vps-os=OS_ID       Vultr OS ID (default: 2136 = Debian 12 x64)\n"
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
    ) -> ProviderInstanceInterface:
        if not isinstance(config, VultrProviderConfig):
            raise MngrError(f"Expected VultrProviderConfig, got {type(config).__name__}")

        try:
            api_key = config.get_api_key()
        except ValueError:
            # No API key configured -- create with empty key.
            # The provider will be discoverable but operations will fail
            # with a clear error message when the API is actually used.
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
