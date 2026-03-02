"""Data types for the mng_recursive plugin."""

from pydantic import Field

from imbue.mng.config.data_types import PluginConfig
from imbue.mng.providers.deploy_utils import MngInstallMode


class RecursivePluginConfig(PluginConfig):
    """Configuration for the mng_recursive plugin."""

    is_errors_fatal: bool = Field(
        default=False,
        description="Whether mng injection failures should abort provisioning",
    )
    install_mode: MngInstallMode = Field(
        default=MngInstallMode.AUTO,
        description="How mng should be installed on remote hosts: auto, package, editable, or skip",
    )

    def merge_with(self, override: "RecursivePluginConfig") -> "RecursivePluginConfig":  # type: ignore[override]
        """Merge this config with an override config.

        Scalar fields: override wins if not None.
        """
        merged_enabled = override.enabled if override.enabled is not None else self.enabled
        merged_is_errors_fatal = (
            override.is_errors_fatal if override.is_errors_fatal is not None else self.is_errors_fatal
        )
        merged_install_mode = override.install_mode if override.install_mode is not None else self.install_mode
        return RecursivePluginConfig(
            enabled=merged_enabled,
            is_errors_fatal=merged_is_errors_fatal,
            install_mode=merged_install_mode,
        )
