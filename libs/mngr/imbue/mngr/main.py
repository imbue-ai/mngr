import bdb
import importlib
import os
import sys
from typing import Any

import click
import pluggy
import setproctitle
from click_option_group import OptionGroup
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
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
from imbue.mngr.utils.click_utils import detect_alias_to_canonical
from imbue.mngr.utils.click_utils import detect_aliases_by_command
from imbue.mngr.utils.env_utils import parse_bool_env

# Module-level container for the plugin manager singleton, created lazily.
# Using a dict avoids the need for the 'global' keyword while still allowing module-level state.
_plugin_manager_container: dict[str, pluggy.PluginManager | None] = {"pm": None}


class _BuiltinSpec(FrozenModel):
    """Specification for a built-in command that is loaded lazily.

    The short_help text is duplicated from the command module's
    CommandHelpMetadata.one_line_description so the root --help can render
    the command list without importing each command module.
    """

    name: str
    module_path: str
    attr_name: str
    short_help: str
    aliases: tuple[str, ...] = Field(default=())
    hidden: bool = False
    apply_plugin_options: bool = True


# Built-in command specs. The `name` is the click command name as it appears
# in `cli.commands` (matches @click.command(name=...) or the function name when
# no explicit name is given). The metadata's `one_line_description` is mirrored
# in `short_help`; if the metadata text changes, update it here as well.
def _spec(
    name: str,
    module_path: str,
    attr_name: str,
    short_help: str,
    *,
    aliases: tuple[str, ...] = (),
    hidden: bool = False,
    apply_plugin_options: bool = True,
) -> _BuiltinSpec:
    return _BuiltinSpec(
        name=name,
        module_path=module_path,
        attr_name=attr_name,
        short_help=short_help,
        aliases=aliases,
        hidden=hidden,
        apply_plugin_options=apply_plugin_options,
    )


_BUILTIN_COMMAND_SPECS: tuple[_BuiltinSpec, ...] = (
    _spec(
        "archive",
        "imbue.mngr.cli.archive",
        "archive",
        "Archive agents (set the 'archived_at' label)",
        apply_plugin_options=False,
    ),
    _spec("ask", "imbue.mngr.cli.ask", "ask", "Chat with mngr for help [experimental]"),
    _spec("capture", "imbue.mngr.cli.capture", "capture", "Capture and display an agent's tmux pane content"),
    _spec(
        "dependencies", "imbue.mngr.cli.check_deps", "check_deps", "Check and install system dependencies", hidden=True
    ),
    _spec(
        "cleanup",
        "imbue.mngr.cli.cleanup",
        "cleanup",
        "Destroy or stop agents and hosts to free up resources [experimental]",
        aliases=("clean",),
    ),
    _spec(
        "clone",
        "imbue.mngr.cli.clone",
        "clone",
        "Create a new agent by cloning an existing one [experimental]",
        apply_plugin_options=False,
    ),
    _spec("config", "imbue.mngr.cli.config", "config", "Manage mngr configuration", aliases=("cfg",)),
    _spec(
        "connect",
        "imbue.mngr.cli.connect",
        "connect",
        "Connect to an existing agent via the terminal",
        aliases=("conn",),
    ),
    _spec("create", "imbue.mngr.cli.create", "create", "Create and run an agent", aliases=("c",)),
    _spec("destroy", "imbue.mngr.cli.destroy", "destroy", "Destroy agent(s) and clean up resources", aliases=("rm",)),
    _spec("events", "imbue.mngr.cli.events", "events", "View events from an agent or host"),
    _spec(
        "exec",
        "imbue.mngr.cli.exec",
        "exec_command",
        "Execute a shell command on one or more agents' hosts",
        aliases=("x",),
    ),
    _spec(
        "extras",
        "imbue.mngr.cli.extras",
        "extras",
        "Install optional extras (plugins, completion, Claude Code plugin)",
        hidden=True,
    ),
    _spec("gc", "imbue.mngr.cli.gc", "gc", "Garbage collect unused resources"),
    _spec("help", "imbue.mngr.cli.help", "help_command", "Show help for a command or topic"),
    _spec("label", "imbue.mngr.cli.label", "label", "Set labels on agents"),
    _spec(
        "limit",
        "imbue.mngr.cli.limit",
        "limit",
        "Configure limits for agents and hosts [experimental]",
        aliases=("lim",),
    ),
    _spec("list", "imbue.mngr.cli.list", "list_command", "List all agents managed by mngr", aliases=("ls",)),
    _spec("message", "imbue.mngr.cli.message", "message", "Send a message to one or more agents", aliases=("msg",)),
    _spec(
        "migrate",
        "imbue.mngr.cli.migrate",
        "migrate",
        "Move an agent to a different host by cloning and destroying the original [experimental]",
        apply_plugin_options=False,
    ),
    _spec(
        "observe", "imbue.mngr.cli.observe", "observe", "Observe agent state changes across all hosts [experimental]"
    ),
    _spec("plugin", "imbue.mngr.cli.plugin", "plugin", "Manage available and active plugins", aliases=("plug",)),
    _spec(
        "provision",
        "imbue.mngr.cli.provision",
        "provision",
        "Re-run provisioning on an existing agent [experimental]",
        aliases=("prov",),
    ),
    _spec(
        "pull",
        "imbue.mngr.cli.pull",
        "pull",
        "Pull files or git commits from an agent to local machine [experimental]",
    ),
    _spec(
        "push",
        "imbue.mngr.cli.push",
        "push",
        "Push files or git commits from local machine to an agent [experimental]",
    ),
    _spec("rename", "imbue.mngr.cli.rename", "rename", "Rename an agent or host [experimental]", aliases=("mv",)),
    _spec(
        "snapshot",
        "imbue.mngr.cli.snapshot",
        "snapshot",
        "Create, list, and destroy host snapshots",
        aliases=("snap",),
    ),
    _spec("start", "imbue.mngr.cli.start", "start", "Start stopped agent(s)"),
    _spec("stop", "imbue.mngr.cli.stop", "stop", "Stop running agent(s)"),
    _spec("transcript", "imbue.mngr.cli.transcript", "transcript", "View the message transcript for an agent"),
)


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
# so repeat lookups are cheap. The cache is intentionally NOT cleared by
# reset_plugin_manager: the click.Command lives on the (cached) command module,
# and re-applying plugin options would duplicate them.
_BUILTINS_BY_NAME: dict[str, _BuiltinSpec] = {}
_BUILTIN_ALIASES_BY_CANONICAL: dict[str, tuple[str, ...]] = {}
_BUILTINS_LOADED: dict[str, click.Command] = {}


def _register_builtin_spec(spec: _BuiltinSpec) -> None:
    _BUILTINS_BY_NAME[spec.name] = spec
    for alias in spec.aliases:
        _BUILTINS_BY_NAME[alias] = spec
    _BUILTIN_ALIASES_BY_CANONICAL[spec.name] = spec.aliases


def get_builtin_alias_to_canonical() -> dict[str, str]:
    """Return a mapping of built-in alias name -> canonical command name.

    Aliases are not stored in ``cli.commands`` because built-in commands are
    loaded lazily; consumers that need the alias list (tab completion cache,
    documentation generators) read it through this helper.
    """
    return {alias: canonical for canonical, aliases in _BUILTIN_ALIASES_BY_CANONICAL.items() for alias in aliases}


def _resolve_builtin(cmd_name: str) -> click.Command | None:
    """Import the module backing `cmd_name` (or its alias) and return the click.Command."""
    spec = _BUILTINS_BY_NAME.get(cmd_name)
    if spec is None:
        return None
    if spec.name not in _BUILTINS_LOADED:
        module = importlib.import_module(spec.module_path)
        real_cmd = getattr(module, spec.attr_name)
        if spec.apply_plugin_options:
            apply_plugin_cli_options(real_cmd, command_name=spec.name)
        if spec.name == "create":
            _update_create_help_with_provider_args()
        _BUILTINS_LOADED[spec.name] = real_cmd
    return _BUILTINS_LOADED[spec.name]


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
        """Return canonical names for built-ins plus all plugin-registered names.

        Aliases are intentionally omitted -- format_commands renders them inline.
        """
        names: set[str] = set(_BUILTIN_ALIASES_BY_CANONICAL.keys())
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
            display_name = ", ".join([canonical, *aliases]) if aliases else canonical
            rows.append((display_name, spec.short_help))

        plugin_alias_to_canonical = detect_alias_to_canonical(self)
        plugin_aliases_by_cmd = detect_aliases_by_command(self)

        plugin_names: list[tuple[str, click.Command]] = []
        for subcommand in super().list_commands(ctx):
            if subcommand in plugin_alias_to_canonical:
                continue
            cmd = super().get_command(ctx, subcommand)
            if cmd is None or cmd.hidden:
                continue
            plugin_names.append((subcommand, cmd))

        if not rows and not plugin_names:
            return

        existing_widths = [len(r[0]) for r in rows]
        plugin_widths = [len(name) for name, _ in plugin_names]
        max_width = max(existing_widths + plugin_widths) if (existing_widths or plugin_widths) else 0
        limit = formatter.width - 6 - max_width

        for subcommand, cmd in plugin_names:
            meta = get_help_metadata(subcommand)
            help_text = meta.one_line_description if meta is not None else cmd.get_short_help_str(limit=limit)
            aliases = plugin_aliases_by_cmd.get(subcommand, [])
            display_name = ", ".join([subcommand, *aliases]) if aliases else subcommand
            rows.append((display_name, help_text))

        if rows:
            rows.sort(key=lambda r: r[0])
            with formatter.section("Commands"):
                formatter.write_dl(rows)


for _builtin in _BUILTIN_COMMAND_SPECS:
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
    # Note: `_BUILTINS_LOADED` is intentionally NOT cleared. Once a built-in
    # command module has been imported and had its plugin options applied, the
    # click.Command object lives on (modules are cached by Python). Re-applying
    # plugin options would duplicate them.


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
