"""TOML-loadable plugin config for ``mngr_latchkey``.

Registered with the ``mngr`` plugin-config registry via
:func:`register_plugin_config` in :mod:`imbue.mngr_latchkey.plugin`. The
two scalar fields here -- ``directory`` and ``latchkey_binary`` -- are
the only things a user can put under ``[plugins.latchkey]`` in
``settings.toml``; everything else the plugin needs (the per-agent
opaque permissions handles, the gateway record, etc.) lives below
``directory`` at runtime.

The CLI reads these values through :func:`resolve_latchkey_settings`,
which applies the documented precedence chain (CLI flag > env var >
settings.toml > built-in default). Both fields are modelled as
``... | None`` because a ``None`` value means "user did not set this
field in their TOML", not "clear it" -- when an override config is
assigned over a base, only fields that were explicitly set take effect.
"""

from pathlib import Path

from pydantic import Field

from imbue.mngr.config.data_types import PluginConfig


class LatchkeyPluginConfig(PluginConfig):
    """Config block under ``[plugins.latchkey]`` in ``settings.toml``."""

    directory: Path | None = Field(
        default=None,
        description=(
            "Root directory passed to spawned ``latchkey`` subprocesses as "
            "``LATCHKEY_DIRECTORY`` and used as the parent of the plugin's "
            "own ``mngr_latchkey/`` metadata subtree. When unset, the CLI "
            "falls back to ``~/.mngr/latchkey``."
        ),
    )
    latchkey_binary: str | None = Field(
        default=None,
        description=(
            "Path to the upstream ``latchkey`` CLI. When unset, the CLI falls back to ``latchkey`` on ``PATH``."
        ),
    )
