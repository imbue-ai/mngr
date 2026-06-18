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

    # NOTE: merge_with is intentionally *not* overridden here. The base
    # PluginConfig.merge_with uses override.model_fields_set, which already
    # gives correct assign-by-default semantics for the forward-specific
    # fields above (only fields the override layer explicitly set win). A
    # hand-written field-by-field merge using ``value is not None`` would both
    # be dead code on the non-optional fields and silently re-enable a plugin
    # the lower layer disabled (an override that does not set ``enabled``
    # defaults it to True). Inherit the base implementation instead.
