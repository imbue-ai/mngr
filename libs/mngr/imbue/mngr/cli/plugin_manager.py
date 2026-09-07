"""The plugin manager every mngr entry point loads its plugins through.

Lives here rather than in :mod:`imbue.mngr.main` so that a caller who needs the
plugin set -- to load config, and from there to reach a provider -- does not
have to import the whole CLI to get at it. Importing ``main`` builds every
command (and loads the plugins as a side effect of doing so), which is a second
of work an embedder that only wants to open a host should not pay for at import
time. The functions here are the whole of that machinery, and they are
re-exported from :mod:`imbue.mngr.main` so the CLI's own callers are unaffected.

It sits in the ``cli`` layer (not ``plugins``) because building the manager
registers the built-in help topics, agents and providers, all of which live
above ``plugins`` in the layer contract.
"""

import os

import pluggy

import imbue.mngr.cli.builtin_help_topics as builtin_help_topics_module
from imbue.mngr.agents.agent_registry import load_agents_from_plugins
from imbue.mngr.config.loader import block_disabled_plugins
from imbue.mngr.config.pre_readers import read_disabled_plugins
from imbue.mngr.plugins import hookspecs
from imbue.mngr.providers.registry import load_all_registries
from imbue.mngr.utils.env_utils import parse_bool_env

# Module-level container for the plugin manager singleton, created lazily.
# Using a dict avoids the need for the 'global' keyword while still allowing module-level state.
_plugin_manager_container: dict[str, pluggy.PluginManager | None] = {"pm": None}


def load_plugin_hookspecs(pm: pluggy.PluginManager) -> None:
    """Register any hookspec modules that plugins return via the register_hookspecs hook."""
    for hookspec_module in pm.hook.register_hookspecs():
        if hookspec_module is not None:
            pm.add_hookspecs(hookspec_module)


def create_plugin_manager() -> pluggy.PluginManager:
    """
    Initializes the plugin manager and loads all plugin registries.

    Plugins disabled in config files are blocked via pm.set_blocked() before
    setuptools entrypoints are loaded, so they are never registered. CLI-level
    --disable-plugin flags are handled later in load_config().

    Setting the MNGR_LOAD_ALL_PLUGINS environment variable skips the
    config-based blocking so that tooling (e.g. doc generation) can load
    every provider regardless of local configuration.

    This should only really be called once from the main command (or during testing).
    """
    # Create plugin manager and load registries first (needed for config parsing)
    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)

    # Block plugins that are disabled in config files. This must happen before
    # load_setuptools_entrypoints so disabled plugins are never registered.
    # MNGR_LOAD_ALL_PLUGINS overrides this so that tooling (e.g. doc generation)
    # can produce output that reflects all providers regardless of local config.
    if not parse_bool_env(os.environ.get("MNGR_LOAD_ALL_PLUGINS", "")):
        block_disabled_plugins(pm, read_disabled_plugins())

    # Automatically discover and load plugins registered via setuptools entry points.
    # External packages can register hooks by adding an entry point for the "mngr" group.
    pm.load_setuptools_entrypoints("mngr")

    # Allow plugins to register their own hookspec modules (for plugin-specific hooks).
    load_plugin_hookspecs(pm)

    # load all classes defined by plugins so they are available later
    load_all_registries(pm)
    load_agents_from_plugins(pm)

    # Register mngr's built-in topics as a built-in plugin (like the backends/
    # agents above). The register_help_topics hook is fired once at module
    # import, not here (see load_help_topics_from_plugins).
    pm.register(builtin_help_topics_module, name="builtin_help_topics")

    return pm


def get_or_create_plugin_manager() -> pluggy.PluginManager:
    """
    Get or create the module-level plugin manager singleton.

    This is used during CLI initialization to apply plugin-registered options
    to commands before argument parsing happens. The singleton ensures that
    plugins are only loaded once even if this is called multiple times.
    """
    if _plugin_manager_container["pm"] is None:
        _plugin_manager_container["pm"] = create_plugin_manager()
    return _plugin_manager_container["pm"]


def reset_plugin_manager() -> None:
    """
    Reset the module-level plugin manager singleton.

    This is primarily useful for testing to ensure a fresh plugin manager
    is created for each test.
    """
    _plugin_manager_container["pm"] = None
