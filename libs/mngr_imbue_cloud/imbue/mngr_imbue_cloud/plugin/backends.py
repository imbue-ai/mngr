from typing import Final

from pydantic import AnyUrl

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.config import get_sessions_dir
from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.connector.session_store import ImbueCloudSessionStore
from imbue.mngr_imbue_cloud.primitives import IMBUE_CLOUD_BACKEND_NAME
from imbue.mngr_imbue_cloud.providers.instance import ImbueCloudProvider
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProvider
from imbue.mngr_imbue_cloud.providers.slice_provider import SliceVpsDockerProviderConfig
from imbue.mngr_imbue_cloud.slices.bare_metal import slice_base_image_file_url
from imbue.mngr_imbue_cloud.slices.lima_slice_client import LimaSliceVpsClient

IMBUE_CLOUD_BACKEND: Final[ProviderBackendName] = ProviderBackendName(IMBUE_CLOUD_BACKEND_NAME)


class ImbueCloudProviderBackend(ProviderBackendInterface):
    """Backend that creates ImbueCloudProvider instances."""

    @staticmethod
    def get_name() -> ProviderBackendName:
        return IMBUE_CLOUD_BACKEND

    @staticmethod
    def get_description() -> str:
        return "Imbue Cloud (leased pool hosts via remote_service_connector)"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return ImbueCloudProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return (
            "Build args constrain which pool host the connector leases for this `mngr create`. "
            "Recognized keys (see LeaseAttributes): repo_url, repo_branch_or_tag, cpus, memory_gb, "
            "gpu_count. Unknown keys are rejected. Example: "
            "`mngr create my-agent@my-host.imbue_cloud_alice --new-host -b cpus=4 -b "
            "repo_branch_or_tag=v1.2.3`."
        )

    @staticmethod
    def get_start_args_help() -> str:
        return "Start args are not used by the imbue_cloud provider."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        if not isinstance(config, ImbueCloudProviderConfig):
            raise MngrError(f"Expected ImbueCloudProviderConfig for instance '{name}', got {type(config).__name__}")
        connector_url = config.get_connector_url()
        client = ImbueCloudConnectorClient(base_url=AnyUrl(connector_url))
        sessions_dir = get_sessions_dir(mngr_ctx.profile_dir)
        session_store = ImbueCloudSessionStore(sessions_dir=sessions_dir)
        return ImbueCloudProvider(
            name=name,
            host_dir=config.host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
            client=client,
            session_store=session_store,
        )


class SliceVpsDockerProviderBackend(ProviderBackendInterface):
    """Backend for the slice provider (lima-VM "VPS" on a bare-metal box).

    Used by the admin bake (``mngr create ...@<host>.imbue_cloud_slice``), run from
    the operator's machine; the lima client drives limactl over SSH on the box.
    """

    @staticmethod
    def get_name() -> ProviderBackendName:
        return ProviderBackendName("imbue_cloud_slice")

    @staticmethod
    def get_description() -> str:
        return "Runs agents in Docker containers inside lima VMs ('slices') on a bare-metal box"

    @staticmethod
    def get_config_class() -> type[ProviderInstanceConfig]:
        return SliceVpsDockerProviderConfig

    @staticmethod
    def get_build_args_help() -> str:
        return (
            "Slice args are passed through to the shared vps_docker bake (e.g. --file=Dockerfile, the build context)."
        )

    @staticmethod
    def get_start_args_help() -> str:
        return "Start args are passed directly to 'docker run' inside the slice VM."

    @staticmethod
    def build_provider_instance(
        name: ProviderInstanceName,
        config: ProviderInstanceConfig,
        mngr_ctx: MngrContext,
    ) -> ProviderInstanceInterface:
        if not isinstance(config, SliceVpsDockerProviderConfig):
            raise MngrError(f"Expected SliceVpsDockerProviderConfig, got {type(config).__name__}")
        # Slices boot from the box-staged guest image (file://) by default so a bake
        # never hits the Debian mirror; an explicit slice_base_image_url overrides it.
        base_image_url = config.slice_base_image_url or slice_base_image_file_url(config.box_ssh_user)
        lima_client = LimaSliceVpsClient(
            box_address=config.box_public_address,
            box_ssh_user=config.box_ssh_user,
            private_key_path=config.pool_private_key_path,
            vm_image_url=base_image_url,
            box_host_public_key=config.box_host_public_key,
        )
        return SliceVpsDockerProvider(
            name=name,
            host_dir=config.host_dir,
            mngr_ctx=mngr_ctx,
            config=config,
            vps_client=lima_client,
            slice_config=config,
            lima_client=lima_client,
        )
