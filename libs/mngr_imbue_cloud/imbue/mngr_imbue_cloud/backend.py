from typing import Final

from pydantic import AnyUrl

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.provider_backend import ProviderBackendInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_imbue_cloud.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.config import ImbueCloudProviderConfig
from imbue.mngr_imbue_cloud.config import get_sessions_dir
from imbue.mngr_imbue_cloud.instance import ImbueCloudProvider
from imbue.mngr_imbue_cloud.primitives import IMBUE_CLOUD_BACKEND_NAME
from imbue.mngr_imbue_cloud.session_store import ImbueCloudSessionStore

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
