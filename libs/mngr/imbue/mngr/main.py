import bdb
import importlib
import os
import sys
from collections.abc import Callable
from collections.abc import Sequence
from typing import Any

import click
import pluggy
import setproctitle
from click_option_group import OptionGroup

from imbue.imbue_common.model_update import to_update
from imbue.mngr.cli.common_opts import TCommand
from imbue.mngr.cli.common_opts import create_group_title_option
from imbue.mngr.cli.common_opts import find_last_option_index_in_group
from imbue.mngr.cli.common_opts import find_option_group
from imbue.mngr.cli.default_command_group import DefaultCommandGroup
from imbue.mngr.cli.help_formatter import get_help_metadata
from imbue.mngr.cli.issue_reporting import handle_not_implemented_error
from imbue.mngr.cli.issue_reporting import handle_unexpected_error
from imbue.mngr.config.loader import block_disabled_plugins
from imbue.mngr.config.pre_readers import read_disabled_plugins
from imbue.mngr.errors import BaseMngrError
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.plugins import hookspecs
from imbue.mngr.providers.registry import get_all_provider_args_help_sections
from imbue.mngr.providers.registry import load_all_registries
from imbue.mngr.utils.builtin_command_specs import BUILTIN_COMMAND_SPECS
from imbue.mngr.utils.builtin_command_specs import BuiltinCommandSpec
from imbue.mngr.utils.click_utils import detect_alias_to_canonical
from imbue.mngr.utils.click_utils import detect_aliases_by_command
from imbue.mngr.utils.env_utils import parse_bool_env

# Module-level container for the plugin manager singleton, created lazily.
# Using a dict avoids the need for the 'global' keyword while still allowing module-level state.
_plugin_manager_container: dict[str, pluggy.PluginManager | None] = {"pm": None}


def _call_on_error_hook(ctx: click.Context, error: BaseException) -> None:
    """Call the on_error hook if command metadata was stored by setup_command_context.

    Note: if a plugin's on_error hook raises, it will mask the original command exception.
    Plugins are responsible for not raising in their hooks.
    """
    command_name = ctx.meta.get("hook_command_name")
    if command_name is not None:
        pm = get_or_create_plugin_manager()
        pm.hook.on_error(
            command_name=command_name,
            command_params=ctx.meta.get("hook_command_params", {}),
            error=error,
        )


# Lazy command registry state.
# `_BUILTINS_BY_NAME` keys are both canonical names and alias names (mapping to the
# same spec). `_BUILTIN_ALIASES_BY_CANONICAL` maps canonical name -> tuple of aliases.
# `_BUILTINS_LOADED` caches resolved click.Command objects keyed by canonical name
# so repeat lookups are cheap. `_BUILTINS_PLUGIN_OPTIONS_APPLIED` separately tracks
# whether plugin options + the post-load hook have already run for a given spec, so
# that if loading is re-entered (e.g. after a hook raised) we never double-apply
# plugin options to the same module-level click.Command. None of these caches is
# cleared by reset_plugin_manager: the click.Command lives on the (cached) command
# module, and re-applying plugin options would duplicate them.
_BUILTINS_BY_NAME: dict[str, BuiltinCommandSpec] = {}
_BUILTIN_ALIASES_BY_CANONICAL: dict[str, tuple[str, ...]] = {}
_BUILTINS_LOADED: dict[str, click.Command] = {}
_BUILTINS_PLUGIN_OPTIONS_APPLIED: set[str] = set()
# Hooks invoked after a built-in command is imported, keyed by canonical name.
# Populated at module bottom once the hook callables are defined.
_BUILTIN_POST_LOAD_HOOKS: dict[str, Callable[[], None]] = {}


def _register_builtin_spec(spec: BuiltinCommandSpec) -> None:
    _BUILTINS_BY_NAME[spec.name] = spec
    for alias in spec.aliases:
        _BUILTINS_BY_NAME[alias] = spec
    _BUILTIN_ALIASES_BY_CANONICAL[spec.name] = spec.aliases


def _resolve_builtin(cmd_name: str) -> click.Command | None:
    """Import the module backing `cmd_name` (or its alias) and return the click.Command."""
    spec = _BUILTINS_BY_NAME.get(cmd_name)
    if spec is None:
        return None
    if spec.name not in _BUILTINS_LOADED:
        module = importlib.import_module(spec.module_path)
        real_cmd = getattr(module, spec.attr_name)
        # Cache the click.Command before mutating it so a later retry never
        # re-imports + re-applies plugin options if the apply step raises.
        _BUILTINS_LOADED[spec.name] = real_cmd
    if spec.name not in _BUILTINS_PLUGIN_OPTIONS_APPLIED:
        # Mark applied BEFORE calling the apply step. If the apply raises, the
        # command is left in a partially-applied state, but a subsequent call
        # won't insert duplicate options on top of the already-mutated cmd.
        _BUILTINS_PLUGIN_OPTIONS_APPLIED.add(spec.name)
        cmd = _BUILTINS_LOADED[spec.name]
        if spec.apply_plugin_options:
            apply_plugin_cli_options(cmd, command_name=spec.name)
        post_load_hook = _BUILTIN_POST_LOAD_HOOKS.get(spec.name)
        if post_load_hook is not None:
            post_load_hook()
    return _BUILTINS_LOADED[spec.name]


def _format_command_display_name(name: str, aliases: Sequence[str]) -> str:
    """Return the "name, alias1, alias2" cell rendered next to a command in --help."""
    if not aliases:
        return name
    return ", ".join([name, *aliases])


class AliasAwareGroup(DefaultCommandGroup):
    """Custom click.Group that shows aliases inline with commands in --help.

    Built-in commands are loaded lazily on first access via the module-level
    `_BUILTINS_*` registry; plugin-registered commands continue to live in
    `self.commands`.

    When no subcommand is given, shows help. Users can configure a default
    subcommand via ``[commands.mngr] default_subcommand`` in config files
    (e.g. set to ``"create"`` to restore the old behavior where
    ``mngr my-task`` is equivalent to ``mngr create my-task``).
    """

    _config_key = "mngr"

    def invoke(self, ctx: click.Context) -> Any:
        try:
            result = super().invoke(ctx)
            # Call on_after_command if command metadata was stored by setup_command_context.
            # Note: if a plugin's on_after_command raises, the exception falls through to
            # the except blocks below, which will call _call_on_error_hook -- meaning
            # on_error fires even though the command itself succeeded. This is intentional
            # for now; plugins are responsible for not raising in their hooks.
            command_name = ctx.meta.get("hook_command_name")
            if command_name is not None:
                pm = get_or_create_plugin_manager()
                pm.hook.on_after_command(
                    command_name=command_name,
                    command_params=ctx.meta.get("hook_command_params", {}),
                )
            return result
        except NotImplementedError as e:
            _call_on_error_hook(ctx, e)
            handle_not_implemented_error(e, is_interactive=ctx.meta.get("is_interactive"))
        except (click.ClickException, click.Abort, click.exceptions.Exit, BaseMngrError, bdb.BdbQuit) as e:
            _call_on_error_hook(ctx, e)
            raise
        except Exception as e:
            _call_on_error_hook(ctx, e)
            if ctx.meta.get("is_error_reporting_enabled", False):
                handle_unexpected_error(e, is_interactive=ctx.meta.get("is_interactive"))
            raise

    def list_commands(self, ctx: click.Context) -> list[str]:
        """Return every command name visible at the root, canonical and alias.

        Built-ins live in the lazy registry, so the click parent class has no
        knowledge of them; we have to add their canonical names and their
        aliases here. Plugin-registered commands and aliases live in
        ``self.commands`` and reach the result through ``super().list_commands``.

        ``format_commands`` renders aliases inline, and consumers that need to
        distinguish canonical names from aliases (e.g., the help overview, the
        completion-cache writer) call ``get_command`` and compare ``cmd.name``
        to the iteration key.
        """
        names: set[str] = set(_BUILTINS_BY_NAME.keys())
        for name in super().list_commands(ctx):
            names.add(name)
        return sorted(names)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        """Return the click.Command for `cmd_name`, importing the built-in module if needed."""
        cmd = super().get_command(ctx, cmd_name)
        if cmd is not None:
            return cmd
        return _resolve_builtin(cmd_name)

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Write the command list with aliases shown inline.

        Built-in command rows are rendered from the lazy registry's `short_help`
        without importing the command module. Plugin-registered commands fall
        back to the standard click metadata + lookup.
        """
        rows: list[tuple[str, str]] = []

        for canonical, aliases in _BUILTIN_ALIASES_BY_CANONICAL.items():
            spec = _BUILTINS_BY_NAME[canonical]
            if spec.hidden:
                continue
            rows.append((_format_command_display_name(canonical, aliases), spec.short_help))

        plugin_alias_to_canonical = detect_alias_to_canonical(self)
        plugin_aliases_by_cmd = detect_aliases_by_command(self)

        # Each plugin entry carries the alias-joined display name alongside the
        # click.Command, so width measurement and short-help truncation see the
        # same rendered string.
        plugin_entries: list[tuple[str, str, click.Command]] = []
        for subcommand in super().list_commands(ctx):
            if subcommand in plugin_alias_to_canonical:
                continue
            cmd = super().get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            aliases = plugin_aliases_by_cmd.get(subcommand, [])
            display_name = _format_command_display_name(subcommand, aliases)
            plugin_entries.append((subcommand, display_name, cmd))

        if not rows and not plugin_entries:
            return

        # Safe: the early return above guarantees at least one of the lists is non-empty.
        builtin_widths = [len(r[0]) for r in rows]
        plugin_widths = [len(display_name) for _, display_name, _ in plugin_entries]
        max_width = max(builtin_widths + plugin_widths)
        limit = formatter.width - 6 - max_width

        for subcommand, display_name, cmd in plugin_entries:
            meta = get_help_metadata(subcommand)
            help_text = meta.one_line_description if meta is not None else cmd.get_short_help_str(limit=limit)
            rows.append((display_name, help_text))

        # Safe: the early return above guarantees rows is non-empty here -- it either
        # already contained built-ins, or just got plugin entries appended above.
        rows.sort(key=lambda r: r[0])
        with formatter.section("Commands"):
            formatter.write_dl(rows)


for _builtin in BUILTIN_COMMAND_SPECS:
    _register_builtin_spec(_builtin)


@click.command(cls=AliasAwareGroup)
@click.version_option(package_name="imbue-mngr", prog_name="mngr", message="%(prog)s %(version)s")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """
    Initial entry point for mngr CLI commands.
    """
    setproctitle.setproctitle(" ".join(["mngr"] + sys.argv[1:]))

    # expose the plugin manager in the command context so that all commands have access to it
    # This uses the singleton that was already created during command registration
    pm = get_or_create_plugin_manager()
    ctx.obj = pm

    pm.hook.on_startup()
    ctx.call_on_close(lambda: pm.hook.on_shutdown())


def _register_plugin_commands() -> list[click.Command]:
    """Register CLI commands from plugins.

    This function is called during module initialization to add any commands
    that plugins have registered via the register_cli_commands hook.

    Returns the list of plugin commands that were registered.
    """
    pm = get_or_create_plugin_manager()
    plugin_commands: list[click.Command] = []

    # Call the hook to get command lists from all plugins
    all_command_lists = pm.hook.register_cli_commands()

    for command_list in all_command_lists:
        if command_list is None:
            continue
        for command in command_list:
            if command.name is None:
                continue
            # Add the plugin command to the CLI group
            cli.add_command(command)
            plugin_commands.append(command)

    return plugin_commands


# Apply plugin-registered CLI options to ALL commands (built-in and plugin).
# This must happen after all commands are added but before the CLI is invoked.
def apply_plugin_cli_options(command: TCommand, command_name: str | None = None) -> TCommand:
    """Apply plugin-registered CLI options to a click command.

    Plugin options are organized into option groups. If a group already exists
    on the command, new options are merged into it. Otherwise, a new group is
    created with a title header for nice help output.
    """
    pm = get_or_create_plugin_manager()
    name = command_name or command.name

    if name is None:
        return command

    # Call the hook to get option mappings from all plugins
    # Each plugin returns a dict of group_name -> list[OptionStackItem]
    all_option_mappings = pm.hook.register_cli_options(command_name=name)

    for option_mapping in all_option_mappings:
        if option_mapping is None:
            continue

        for group_name, option_specs in option_mapping.items():
            existing_group = find_option_group(command, group_name)

            if existing_group is not None:
                # Add options to existing group after the last option in that group
                insert_index = find_last_option_index_in_group(command, existing_group) + 1
                for option_spec in option_specs:
                    click_option = option_spec.to_click_option(group=existing_group)
                    # Register option with the group for proper help rendering
                    existing_group._options[command.callback][click_option.name] = click_option
                    command.params.insert(insert_index, click_option)
                    insert_index += 1
            else:
                # Create new group with title option for help rendering
                new_group = OptionGroup(group_name)
                title_option = create_group_title_option(new_group)
                command.params.append(title_option)

                for option_spec in option_specs:
                    click_option = option_spec.to_click_option(group=new_group)
                    # Register option with the group for proper help rendering
                    new_group._options[command.callback][click_option.name] = click_option
                    command.params.append(click_option)

    return command


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
    # Imported here to keep `imbue.mngr.main` import lightweight; `agent_registry`
    # transitively pulls heavy modules (modal, rich) that aren't needed before the
    # plugin manager is actually constructed.
    from imbue.mngr.agents.agent_registry import load_agents_from_plugins

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

    # Wire up the agent type resolver so hosts can resolve agent types
    # without directly importing from the agents layer

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
    # Note: `_BUILTINS_LOADED` and `_BUILTINS_PLUGIN_OPTIONS_APPLIED` are
    # intentionally NOT cleared. Once a built-in command module has been imported
    # and had its plugin options applied, the click.Command object lives on
    # (modules are cached by Python). Re-applying plugin options would duplicate
    # them.


# Register plugin commands. This eagerly creates the plugin manager and triggers
# setuptools entry-point loading so plugin-registered commands appear in --help.
# Wrapped in try/except because this runs at module import time, before Click's
# exception handling is active, so ConfigParseError would produce a stack trace.
try:
    PLUGIN_COMMANDS: list[click.Command] = _register_plugin_commands()
except ConfigParseError as e:
    e.show()
    sys.exit(1)

# Plugin CLI options for built-in commands are applied lazily inside
# `_resolve_builtin` -- only the plugin commands need eager option wiring here,
# since they're already in `cli.commands`.
for _plugin_cmd in PLUGIN_COMMANDS:
    apply_plugin_cli_options(_plugin_cmd)


def _update_create_help_with_provider_args() -> None:
    """Update the create command's help metadata with provider-specific build/start args help.

    This must be called after backends are loaded so that all provider backends
    are registered and their help text is available.
    """
    provider_sections = get_all_provider_args_help_sections()
    existing_metadata = get_help_metadata("create")
    if existing_metadata is None:
        return
    updated_metadata = existing_metadata.model_copy_update(
        to_update(
            existing_metadata.field_ref().additional_sections,
            existing_metadata.additional_sections + provider_sections,
        ),
    )
    updated_metadata.register()


# Register post-load hooks once the callables are defined. The mapping is
# consulted by `_resolve_builtin`, which only runs at first command lookup, so
# populating it at module bottom is fine.
_BUILTIN_POST_LOAD_HOOKS["create"] = _update_create_help_with_provider_args
