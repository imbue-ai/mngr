from typing import Any
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.logging import log_span
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_vps_docker.errors import ContainerSetupError
from imbue.mngr_vps_docker.errors import VpsConnectionError
from imbue.mngr_vps_docker.host_store import VpsDockerHostRecord
from imbue.mngr_vps_docker.instance import VpsDockerProvider
from imbue.mngr_vultr import hookimpl
from imbue.mngr_vultr.client import VultrVpsClient
from imbue.mngr_vultr.config import VultrProviderConfig

VULTR_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("vultr")


class VultrProvider(VpsDockerProvider):
    """Vultr-specific provider that overrides discovery to use the Vultr API."""

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

    def _get_tagged_vps_ips(self) -> list[str]:
        """Get IPs of Vultr instances tagged with this provider's name."""
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

    def _read_records_from_vps(
        self,
        vps_ip: str,
    ) -> tuple[list[VpsDockerHostRecord], dict[HostId, list[dict[str, Any]]]]:
        """Read all host records and agent data from a single VPS in one SSH command.

        Uses the read-only host store so that discovery never creates the
        state container. If the container does not exist yet (e.g., the VPS
        is still being set up by a concurrent ``mngr create``), returns
        empty results.
        """
        try:
            docker_ssh = self._make_docker_ssh(vps_ip)
            host_store = self._get_existing_host_store(docker_ssh)
            if host_store is None:
                logger.debug("State container not ready on VPS {}, skipping", vps_ip)
                return [], {}
            return host_store.list_all_host_records_with_agents()
        except (VpsConnectionError, ContainerSetupError) as e:
            logger.warning("Failed to read records from VPS {}: {}", vps_ip, e)
            return [], {}

    def _discover_host_records_with_agents(
        self,
    ) -> tuple[list[VpsDockerHostRecord], dict[HostId, list[dict[str, Any]]]]:
        """Discover host records and agent data from all Vultr VPSes.

        Queries the Vultr API for tagged instances, then SSHes to each VPS
        in parallel to read host records and agent data in a single command.
        """
        vps_ips = self._get_tagged_vps_ips()
        if not vps_ips:
            return [], {}

        all_records: list[VpsDockerHostRecord] = []
        all_agent_data: dict[HostId, list[dict[str, Any]]] = {}

        # SSH to all VPSes in parallel
        with log_span("Reading records from {} VPS instance(s) in parallel", len(vps_ips)):
            cg = ConcurrencyGroup(name="vultr-discover")
            with cg:
                with ConcurrencyGroupExecutor(
                    parent_cg=cg,
                    name="vultr_read_records",
                    max_workers=min(len(vps_ips), 32),
                ) as executor:
                    futures = [executor.submit(self._read_records_from_vps, ip) for ip in vps_ips]

                for future in futures:
                    records, agent_data = future.result()
                    all_records.extend(records)
                    for host_id, agents in agent_data.items():
                        all_agent_data.setdefault(host_id, []).extend(agents)

        return all_records, all_agent_data

    def _discover_host_records(self) -> list[VpsDockerHostRecord]:
        """Discover host records by querying the Vultr API for tagged instances."""
        records, _agent_data = self._discover_host_records_with_agents()
        return records

    def _find_host_record(self, host: HostId | HostName) -> VpsDockerHostRecord | None:
        """Find a host record by ID or name, using cache first."""
        # Check cache first
        if isinstance(host, HostId) and host in self._host_record_cache:
            return self._host_record_cache[host]
        if isinstance(host, HostName):
            for cached_record in self._host_record_cache.values():
                if cached_record.certified_host_data.host_name == str(host):
                    return cached_record

        if not self.vultr_client.api_key.get_secret_value():
            logger.warning("Vultr API key not configured, cannot resolve host")
            return None

        # Fall back to full discovery
        records = self._discover_host_records()
        for record in records:
            host_id = HostId(record.certified_host_data.host_id)
            self._host_record_cache[host_id] = record
            if isinstance(host, HostId) and record.certified_host_data.host_id == str(host):
                return record
            elif isinstance(host, HostName) and record.certified_host_data.host_name == str(host):
                return record
        return None


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
    ) -> ProviderInstanceInterface:
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
