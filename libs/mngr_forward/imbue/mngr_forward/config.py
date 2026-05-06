"""TOML-loadable plugin config for ``mngr_forward``.

Mirrors the convention used by other ``libs/mngr_*`` plugins: a
``PluginConfig`` subclass registered via ``register_plugin_config(...)``,
mergeable with the base entry from ``mngr``'s root config.
"""

from pydantic import Field

from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr_forward.primitives import ForwardPort


class ForwardPluginConfig(PluginConfig):
    """Config block under ``[plugins.forward]`` in ``settings.toml``."""

    port: ForwardPort = Field(
        default=ForwardPort(8421),
        description="Default bind port for ``mngr forward`` when --port is not passed.",
    )
    agent_include: str | None = Field(
        default=None,
        description="Default --agent-include CEL expression. CLI flag takes precedence.",
    )
    agent_exclude: str | None = Field(
        default=None,
        description="Default --agent-exclude CEL expression. CLI flag takes precedence.",
    )
    event_include: str | None = Field(
        default=None,
        description="Default --event-include CEL expression. CLI flag takes precedence.",
    )
    event_exclude: str | None = Field(
        default=None,
        description="Default --event-exclude CEL expression. CLI flag takes precedence.",
    )
    auto_open_browser: bool = Field(
        default=False,
        description="Whether to open the login URL automatically (sets --open-browser by default).",
    )

    def merge_with(self, override: "PluginConfig") -> "ForwardPluginConfig":
        merged_enabled = override.enabled if override.enabled is not None else self.enabled
        if not isinstance(override, ForwardPluginConfig):
            return self.__class__(
                enabled=merged_enabled,
                port=self.port,
                agent_include=self.agent_include,
                agent_exclude=self.agent_exclude,
                event_include=self.event_include,
                event_exclude=self.event_exclude,
                auto_open_browser=self.auto_open_browser,
            )
        return self.__class__(
            enabled=merged_enabled,
            port=override.port if override.port is not None else self.port,
            agent_include=override.agent_include if override.agent_include is not None else self.agent_include,
            agent_exclude=override.agent_exclude if override.agent_exclude is not None else self.agent_exclude,
            event_include=override.event_include if override.event_include is not None else self.event_include,
            event_exclude=override.event_exclude if override.event_exclude is not None else self.event_exclude,
            auto_open_browser=(
                override.auto_open_browser if override.auto_open_browser is not None else self.auto_open_browser
            ),
        )
