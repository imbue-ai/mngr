"""Static metadata for the built-in CLI commands that ``imbue.mngr.main`` loads
lazily.

Lives in the ``utils`` layer so consumers below ``main`` (e.g.
``imbue.mngr.config.completion_writer``) can read the alias mapping without
violating the import-linter layer contract. ``main`` owns the actual lazy
resolution machinery; this module is pure data.
"""

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class BuiltinCommandSpec(FrozenModel):
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


def _spec(
    name: str,
    module_path: str,
    attr_name: str,
    short_help: str,
    *,
    aliases: tuple[str, ...] = (),
    hidden: bool = False,
    apply_plugin_options: bool = True,
) -> BuiltinCommandSpec:
    return BuiltinCommandSpec(
        name=name,
        module_path=module_path,
        attr_name=attr_name,
        short_help=short_help,
        aliases=aliases,
        hidden=hidden,
        apply_plugin_options=apply_plugin_options,
    )


# Built-in command specs. The ``name`` is the click command name as it appears
# in ``cli.commands`` (matches @click.command(name=...) or the function name when
# no explicit name is given). The metadata's ``one_line_description`` is mirrored
# in ``short_help``; the ``test_builtin_specs_match_command_help_metadata`` test
# in ``main_test.py`` enforces that the two stay in sync.
BUILTIN_COMMAND_SPECS: tuple[BuiltinCommandSpec, ...] = (
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


def get_builtin_alias_to_canonical() -> dict[str, str]:
    """Return a mapping of built-in alias name -> canonical command name.

    Aliases are not stored in ``cli.commands`` because built-in commands are
    loaded lazily; consumers that need the alias list (tab completion cache,
    documentation generators) read it through this helper.
    """
    return {alias: spec.name for spec in BUILTIN_COMMAND_SPECS for alias in spec.aliases}
