"""Data types for the mngr_recursive plugin."""

from pydantic import Field

from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.providers.deploy_utils import MngrInstallMode


class RecursivePluginConfig(PluginConfig):
    """Configuration for the mngr_recursive plugin."""

    is_errors_fatal: bool = Field(
        default=False,
        description="Whether mngr injection failures should abort provisioning",
    )
    install_mode: MngrInstallMode = Field(
        default=MngrInstallMode.AUTO,
        description="How mngr should be installed on remote hosts: auto, package, editable, or skip",
    )
