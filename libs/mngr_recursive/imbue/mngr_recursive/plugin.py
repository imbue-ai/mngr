"""Plugin registration for mngr_recursive."""

from imbue.mngr import hookimpl
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.plugin_registry import register_plugin_config
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr_recursive.data_types import RecursivePluginConfig
from imbue.mngr_recursive.provisioning import provision_mngr_on_host

register_plugin_config("recursive", RecursivePluginConfig)


@hookimpl
def on_host_created(host: OnlineHostInterface, mngr_ctx: MngrContext) -> None:
    """Provision host-level mngr prerequisites (deploy files, uv availability)."""
    provision_mngr_on_host(host=host, mngr_ctx=mngr_ctx)
