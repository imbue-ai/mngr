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

    # NOTE: merge_with is deliberately NOT overridden. The base PluginConfig.merge_with
    # uses ``model_fields_set`` so that an override layer (e.g. project settings) only
    # overrides the fields it explicitly set, leaving the rest inherited from the base
    # layer (e.g. user settings). is_errors_fatal and install_mode are plain (non-None)
    # fields, so a previous hand-rolled "override wins if not None" override clobbered
    # them with the override layer's *defaults* even when that layer never set them --
    # silently dropping the base layer's values. The inherited implementation handles
    # subclass fields correctly, so there is nothing to add here.
