from collections.abc import Sequence
from pathlib import Path
from typing import Final

import ovh
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

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
from imbue.mngr_ovh import hookimpl
from imbue.mngr_ovh.bootstrap import pin_host_key_via_tofu
from imbue.mngr_ovh.bootstrap import wait_for_ssh_after_rebuild
from imbue.mngr_ovh.catalog import resolve_image_id
from imbue.mngr_ovh.client import OvhVpsClient
from imbue.mngr_ovh.config import OvhProviderConfig
from imbue.mngr_ovh.iam_tags import MNGR_HOST_ID_TAG_KEY
from imbue.mngr_ovh.iam_tags import MNGR_PROVIDER_TAG_KEY
from imbue.mngr_ovh.iam_tags import attach_tags
from imbue.mngr_ovh.iam_tags import list_vps_resources_for_provider
from imbue.mngr_ovh.iam_tags import vps_urn_for
from imbue.mngr_ovh.ordering import order_and_wait_for_vps
from imbue.mngr_ovh.ordering import rebuild_vps_with_public_key
from imbue.mngr_vps_docker.errors import VpsApiError
from imbue.mngr_vps_docker.instance import ParsedVpsBuildOptions
from imbue.mngr_vps_docker.instance import VpsDockerProvider
from imbue.mngr_vps_docker.primitives import VpsInstanceId

OVH_BACKEND_NAME: Final[ProviderBackendName] = ProviderBackendName("ovh")

_OVH_REBUILD_TASK_TIMEOUT_SECONDS: Final[float] = 1800.0


class OvhProvider(VpsDockerProvider):
    """OVH classic-VPS provider built on top of ``VpsDockerProvider``.

    Implements the provider-specific VPS listing via OVH IAM v2 tags and
    overrides ``_provision_vps`` with OVH's order + rebuild + TOFU flow,
    since OVH classic VPS has no cloud-init / userData support and uses a
    multi-step order/cart purchase rather than a single-POST instance API.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ovh_client: OvhVpsClient = Field(frozen=True, description="OVH API client")
    ovh_config: OvhProviderConfig = Field(frozen=True, description="OVH-specific configuration")

    _vps_iam_cache: list[str] | None = PrivateAttr(default=None)

    def reset_caches(self) -> None:
        super().reset_caches()
        self._vps_iam_cache = None

    # =========================================================================
    # Build-args parsing -- OVH uses string image names and --vps-datacenter
    # =========================================================================

    def _parse_build_args(self, build_args: Sequence[str] | None) -> ParsedVpsBuildOptions:
        region = self.config.default_region
        plan = self.config.default_plan
        os_id: int | str = self.ovh_config.default_image_name
        git_depth: int | None = None
        docker_build_args: list[str] = []
        if build_args:
            for arg in build_args:
                if arg.startswith("--vps-datacenter="):
                    region = arg.split("=", 1)[1]
                elif arg.startswith("--vps-region="):
                    region = arg.split("=", 1)[1]
                elif arg.startswith("--vps-plan="):
                    plan = arg.split("=", 1)[1]
                elif arg.startswith("--vps-os="):
                    os_id = arg.split("=", 1)[1]
                elif arg.startswith("--git-depth="):
                    git_depth = int(arg.split("=", 1)[1])
                elif arg.startswith("--vps-"):
                    raise MngrError(
                        f"Unknown OVH build arg: {arg}. "
                        "Valid args: --vps-datacenter=, --vps-plan=, --vps-os=, --git-depth="
                    )
                else:
                    docker_build_args.append(arg)
        return ParsedVpsBuildOptions(
            region=region,
            plan=plan,
            os_id=os_id,
            git_depth=git_depth,
            docker_build_args=tuple(docker_build_args),
        )

    # =========================================================================
    # Discovery -- list our VPSes via IAM v2 tags
    # =========================================================================

    def _list_provider_vps_ips(self) -> list[str]:
        if self._vps_iam_cache is not None:
            return list(self._vps_iam_cache)
        try:
            resources = list_vps_resources_for_provider(self.ovh_client, provider_name=str(self.name))
        except (VpsApiError, MngrError) as e:
            # Includes the "no credentials" case: python-ovh raises
            # InvalidCredential / NotCredential from _call, which we then
            # remap to VpsApiError. Treating this as an empty discovery
            # (with a warning) keeps `mngr list` from crashing when OVH is
            # configured but unreachable, while still surfacing the cause
            # in the logs. Discovery from ~/.ovh.conf works through the
            # same path.
            logger.warning("OVH IAM tag listing failed; treating as empty: {}", e)
            self._vps_iam_cache = []
            return []
        vps_ips = [r.name for r in resources if r.name]
        self._vps_iam_cache = vps_ips
        return list(vps_ips)

    # =========================================================================
    # VPS provisioning -- OVH order + rebuild + TOFU + IAM tag attach
    # =========================================================================

    def _provision_vps(
        self,
        host_id: HostId,
        name: HostName,
        region: str,
        plan: str,
        os_id: int | str,
        vps_host_key_path: Path,
        vps_host_public_key: str,
        vps_ssh_key_id: str,
    ) -> tuple[VpsInstanceId, str]:
        """Drive the OVH classic-VPS provisioning flow.

        Unlike the Vultr-shaped base implementation, this:
        1. Orders a fresh VPS via the order/cart API (multi-step, ~minutes).
        2. Rebuilds it with our local SSH public key pre-installed.
        3. Pins the SSH host key on first connect (TOFU; see README caveat).
        4. Attaches the ``mngr-provider`` / ``mngr-host-id`` IAM tags used
           for cross-process discovery.

        ``vps_host_key_path`` / ``vps_host_public_key`` are accepted but
        unused: OVH provides no mechanism to inject host keys at install
        time. The locally-generated host key files are left on disk; they
        do no harm and keep the base ``create_host`` flow uniform.

        Returns the OVH ``serviceName`` for both the instance id *and* the
        SSH-reachable hostname -- OVH's serviceName is itself a DNS name
        like ``vps-eec8860b.vps.ovh.us`` that resolves to the VPS's IP.
        """
        del vps_host_key_path, vps_host_public_key
        image_name = str(os_id)

        public_key = self.ovh_client.get_cached_public_key(vps_ssh_key_id)

        with log_span("OVH provisioning for host {} ({})", name, host_id):
            service_name = order_and_wait_for_vps(
                self.ovh_client,
                plan_code=plan,
                datacenter=region,
                image_name=image_name,
                pricing_mode=self.ovh_config.pricing_mode.to_wire_value(),
                duration=self.ovh_config.duration,
                deliver_timeout_seconds=self.ovh_config.vps_boot_timeout,
            )

            image_id = resolve_image_id(self.ovh_client, service_name, image_name)
            rebuild_vps_with_public_key(
                self.ovh_client,
                service_name=service_name,
                image_id=image_id,
                public_ssh_key=public_key,
                task_timeout_seconds=_OVH_REBUILD_TASK_TIMEOUT_SECONDS,
            )

            wait_for_ssh_after_rebuild(
                hostname=service_name,
                port=22,
                timeout_seconds=self.config.ssh_connect_timeout,
            )

            vps_private_key_path, _ = self._get_vps_ssh_keypair()
            pin_host_key_via_tofu(
                hostname=service_name,
                port=22,
                ssh_user="root",
                private_key_path=vps_private_key_path,
                known_hosts_path=self._vps_known_hosts_path(),
                timeout_seconds=self.config.ssh_connect_timeout,
            )

            urn = vps_urn_for(service_name, region_code=_iam_region_code(self.ovh_config.endpoint))
            attach_tags(
                self.ovh_client,
                urn,
                {
                    MNGR_PROVIDER_TAG_KEY: str(self.name),
                    MNGR_HOST_ID_TAG_KEY: str(host_id),
                },
            )

        return VpsInstanceId(service_name), service_name


def _iam_region_code(endpoint: str) -> str:
    """Map a python-ovh endpoint id (``ovh-us``) to the URN's region segment (``us``)."""
    if endpoint.startswith("ovh-"):
        return endpoint.removeprefix("ovh-")
    return "us"


def _build_ovh_client(config: OvhProviderConfig) -> OvhVpsClient:
    """Construct an authenticated ``OvhVpsClient`` from config / env / ~/.ovh.conf."""
    kwargs = config.resolve_python_ovh_kwargs()
    raw_client = ovh.Client(**kwargs)
    return OvhVpsClient(ovh_client=raw_client, subsidiary=config.ovh_subsidiary)


class OvhProviderBackend(ProviderBackendInterface):
    """Backend for creating OVH classic-VPS Docker provider instances."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return OVH_BACKEND_NAME

    @staticmethod
    def get_description() -> str:
        return "Runs agents in Docker containers on OVH classic VPS instances"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return OvhProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return (
            "VPS-specific args (consumed by provider, not passed to docker):\n"
            "  --vps-datacenter=DC   OVH datacenter (e.g. US-EAST-VA, US-WEST-OR)\n"
            "  --vps-plan=PLAN       OVH plan code (default: vps-2025-model1 = VPS-1)\n"
            "  --vps-os=NAME         OVH image name (default: 'Debian 12 - Docker')\n"
            "  --git-depth=N         Shallow-clone build context to depth N before upload\n"
            "\n"
            "All other build args are passed to 'docker build' on the VPS.\n"
            "Example: -b --vps-plan=vps-2025-model1 -b --file=Dockerfile -b .\n"
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
        if not isinstance(config, OvhProviderConfig):
            raise MngrError(f"Expected OvhProviderConfig, got {type(config).__name__}")
        ovh_client = _build_ovh_client(config)
        return OvhProvider(
            name=name,
            host_dir=config.host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
            vps_client=ovh_client,
            ovh_client=ovh_client,
            ovh_config=config,
        )


@hookimpl
def register_provider_backend() -> tuple[type[ProviderBackendInterface], type[ProviderInstanceConfig]]:
    """Register the OVH provider backend."""
    return (OvhProviderBackend, OvhProviderConfig)
