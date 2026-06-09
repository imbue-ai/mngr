import os
from typing import Any
from typing import Final

from google.auth import exceptions as google_auth_exceptions
from pydantic import ConfigDict
from pydantic import Field

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import ProviderEmptyError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_gcp import hookimpl
from imbue.mngr_gcp.client import GcpVpsClient
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_vps_docker.instance import VpsDockerProvider

GCP_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("gcp")


class GcpProvider(VpsDockerProvider):
    """GCP-specific provider that discovers hosts via the GCE instances.list API."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    gcp_client: GcpVpsClient = Field(frozen=True, description="GCE API client")
    gcp_config: GcpProviderConfig = Field(frozen=True, description="GCP-specific configuration")

    def _fetch_provider_instances(self) -> list[dict[str, Any]]:
        """List GCE instances labeled with this provider's name."""
        return self.gcp_client.list_instances(provider_tag=str(self.name))

    def _validate_provider_args_for_create(self) -> None:
        """Refuse to create a GCE instance under pytest without auto_shutdown_minutes set.

        Mirrors the AWS guard (``mngr_aws.backend.AwsProvider``): when
        ``PYTEST_CURRENT_TEST`` is set, the test harness is responsible for
        configuring the safety net that prevents leaked cost if pytest itself is
        killed. For GCP, that net is ``scheduling.max_run_duration`` +
        ``instance_termination_action=DELETE`` (both rely on
        ``auto_shutdown_minutes`` being set on the provider config). If it
        isn't, fail closed at the pre-create hook rather than silently leak an
        instance.
        """
        if "PYTEST_CURRENT_TEST" not in os.environ:
            return
        minutes = self._get_effective_auto_shutdown_minutes()
        if not (minutes and minutes > 0):
            raise MngrError(
                "Refusing to create GCE instance during pytest without "
                "auto_shutdown_minutes set on the GCP provider config. "
                "Set [providers.<instance>] auto_shutdown_minutes = <N> in "
                "the project settings.toml so the instance launches with "
                "scheduling.max_run_duration + instance_termination_action=DELETE "
                "and self-deletes even if pytest is killed."
            )

    def _list_provider_vps_hostnames(self) -> list[str]:
        """Return external IPs of GCE instances labeled with this provider's name.

        Credentials are guaranteed to be resolvable here: ``build_provider_instance``
        raises ``ProviderEmptyError`` when ``config.get_credentials()`` fails, so any
        GcpProvider that reaches this point has working credentials.
        """
        instances = self._list_instances_cached()
        vps_ips: list[str] = []
        for instance in instances:
            main_ip = instance.get("main_ip", "")
            if main_ip:
                vps_ips.append(main_ip)
        return vps_ips


class GcpProviderBackend(ProviderBackendInterface):
    """Backend for creating GCP Compute Engine VPS Docker provider instances."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return GCP_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents in Docker containers on GCP Compute Engine VMs"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return GcpProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return (
            "GCE-specific args (consumed by provider, not passed to docker):\n"
            "  --vps-region=ZONE    GCE zone, e.g. us-west1-a (GCE VMs are zonal; default: us-west1-a)\n"
            "  --vps-plan=TYPE      GCE machine type (default: e2-small)\n"
            "  --git-depth=N        Shallow-clone build context to depth N before upload\n"
            "\n"
            "Image is taken from the provider config (default_image); per-host image\n"
            "overrides are not supported via build args.\n"
            "\n"
            "All other build args are passed to 'docker build' on the GCE instance.\n"
            "Example: -b --vps-plan=e2-medium -b --file=Dockerfile -b .\n"
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
        if not isinstance(config, GcpProviderConfig):
            raise MngrError(f"Expected GcpProviderConfig, got {type(config).__name__}")

        try:
            credentials = config.get_credentials()
            project_id = config.get_project_id()
            config.validate_zone_in_region()
        except (ValueError, google_auth_exceptions.GoogleAuthError) as e:
            # Match the AWS/Modal pattern: when the provider cannot be reached
            # (no resolvable ADC, no project_id, or a malformed zone/region),
            # raise ProviderEmptyError so read paths (mngr list / mngr gc /
            # discovery) skip the GCP provider entirely instead of constructing
            # a half-working placeholder. Host-creation paths surface this same
            # error to the user.
            raise ProviderEmptyError(name, str(e)) from e

        gcp_client = GcpVpsClient(
            credentials=credentials,
            project_id=project_id,
            zone=config.default_zone,
            image=config.default_image,
            machine_type=config.default_plan,
            boot_disk_size_gb=config.boot_disk_size_gb,
            boot_disk_type=config.boot_disk_type,
            network=config.network,
            subnetwork=config.subnetwork,
            allowed_ssh_cidrs=config.allowed_ssh_cidrs,
            firewall_name=config.firewall_name,
            firewall_target_tag=config.firewall_target_tag,
            associate_external_ip=config.associate_external_ip,
            service_account_email=config.service_account_email,
            service_account_scopes=config.service_account_scopes,
            auto_shutdown_minutes=config.auto_shutdown_minutes,
            container_ssh_port=config.container_ssh_port,
        )

        return GcpProvider(
            name=name,
            host_dir=config.host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
            vps_client=gcp_client,
            gcp_client=gcp_client,
            gcp_config=config,
        )


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the GCP provider backend."""
    return (GcpProviderBackend, GcpProviderConfig)
