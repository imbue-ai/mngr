"""TOML-loadable plugin config for ``mngr_foreman``.

Mirrors the convention used by other ``libs/mngr_*`` plugins: a
``PluginConfig`` subclass registered via ``register_plugin_config(...)``,
mergeable with the base entry from ``mngr``'s root config (block
``[plugins.foreman]`` in ``settings.toml``).
"""

from pydantic import Field

from imbue.mngr.config.data_types import PluginConfig


class ForemanPluginConfig(PluginConfig):
    """Config block under ``[plugins.foreman]`` in ``settings.toml``."""

    port: int = Field(
        default=8700,
        description="Default bind port for ``mngr foreman serve`` when --port is not passed.",
    )
    host: str = Field(
        default="0.0.0.0",
        description="Default bind host. 0.0.0.0 exposes on the LAN/tailnet; there is no auth by design.",
    )
    max_tool_output_chars: int = Field(
        default=20000,
        description="Cap on tool_result output length in the transcript parser. 0 means unlimited.",
    )
    foreman_only: bool = Field(
        default=False,
        description="If true, only show agents labelled foreman=1. CLI --foreman-only overrides.",
    )
