"""Plugin registration for mng_recursive."""

from imbue.mng import hookimpl
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.plugin_registry import register_plugin_config
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng_recursive.data_types import RecursivePluginConfig
from imbue.mng_recursive.provisioning import provision_mng_on_host

register_plugin_config("recursive", RecursivePluginConfig)


@hookimpl
def on_after_provisioning(agent: AgentInterface, host: OnlineHostInterface, mng_ctx: MngContext) -> None:
    """Inject mng config, settings, and dependencies into remote hosts after provisioning."""
    provision_mng_on_host(host=host, mng_ctx=mng_ctx)
