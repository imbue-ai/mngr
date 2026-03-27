from pydantic import Field

from imbue.mngr.config.data_types import PluginConfig


class NotificationsPluginConfig(PluginConfig):
    """Configuration for the notifications plugin.

    Example settings.toml:

        [plugins.notifications]
        terminal_app = "iTerm"

    Or with a custom command:

        [plugins.notifications]
        custom_terminal_command = "open -a MyTerminal --args mngr connect $MNGR_AGENT_NAME"
    """

    notification_only: bool = Field(
        default=False,
        description="Send plain notifications without click-to-connect terminal integration.",
    )
    terminal_app: str | None = Field(
        default=None,
        description="Terminal application for click-to-connect. Supported: iTerm, Terminal, WezTerm, Kitty",
    )
    custom_terminal_command: str | None = Field(
        default=None,
        description="Custom shell command to run on notification click. "
        "$MNGR_AGENT_NAME is set in the environment to the agent's name.",
    )

    def merge_with(self, override: "PluginConfig") -> "NotificationsPluginConfig":
        """Merge this config with an override config."""
        if not isinstance(override, NotificationsPluginConfig):
            return self
        return NotificationsPluginConfig(
            enabled=override.enabled if override.enabled is not None else self.enabled,
            notification_only=override.notification_only
            if override.notification_only is not None
            else self.notification_only,
            terminal_app=override.terminal_app if override.terminal_app is not None else self.terminal_app,
            custom_terminal_command=override.custom_terminal_command
            if override.custom_terminal_command is not None
            else self.custom_terminal_command,
        )
