"""Help command and standalone topic pages for the mngr CLI.

Provides two types of help:
1. Command help: ``mngr help create`` is equivalent to ``mngr create --help``
2. Topic help: ``mngr help address`` shows a standalone documentation page

Both commands and topics support aliases (e.g., ``mngr help c`` for create,
``mngr help addr`` for address).
"""

import sys
from io import StringIO

import click
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.help_formatter import format_git_style_help
from imbue.mngr.cli.help_formatter import get_all_help_metadata
from imbue.mngr.cli.help_formatter import get_help_metadata
from imbue.mngr.cli.help_formatter import run_pager
from imbue.mngr.config.data_types import MngrConfig

# =============================================================================
# Topic help page data model and registry
# =============================================================================


class TopicHelpPage(FrozenModel):
    """A standalone help topic page (not associated with any CLI command).

    Topic pages document concepts that span multiple commands, such as
    filter syntax or agent address format.
    """

    key: str = Field(description="Topic identifier (e.g., 'filter')")
    one_line_description: str = Field(description="Brief one-line description")
    content: str = Field(description="Full content of the topic page")
    aliases: tuple[str, ...] = Field(default=(), description="Topic aliases (e.g., ('addr',) for 'address')")
    see_also: tuple[tuple[str, str], ...] = Field(
        default=(), description="See Also references as (name, description) tuples"
    )

    def register(self) -> None:
        """Register this topic page in the global topic registry."""
        _topic_registry[self.key] = self
        for alias in self.aliases:
            _topic_alias_to_canonical[alias] = self.key


_topic_registry: dict[str, TopicHelpPage] = {}
_topic_alias_to_canonical: dict[str, str] = {}


def get_topic(name: str) -> TopicHelpPage | None:
    """Look up a topic by name or alias."""
    canonical = _topic_alias_to_canonical.get(name, name)
    return _topic_registry.get(canonical)


def get_all_topics() -> dict[str, TopicHelpPage]:
    """Return a copy of the topic registry."""
    return dict(_topic_registry)


# =============================================================================
# Formatting
# =============================================================================


@pure
def format_topic_help(topic: TopicHelpPage) -> str:
    """Format a topic help page in git-style man-page format."""
    output = StringIO()

    # NAME section
    output.write("NAME\n")
    name_str = topic.key
    if topic.aliases:
        name_str += f" ({', '.join(topic.aliases)})"
    output.write(f"       {name_str} - {topic.one_line_description}\n")
    output.write("\n")

    # DESCRIPTION section
    output.write("DESCRIPTION\n")
    for line in topic.content.strip().split("\n"):
        if line.strip():
            output.write(f"       {line}\n")
        else:
            output.write("\n")
    output.write("\n")

    # SEE ALSO section
    if topic.see_also:
        output.write("SEE ALSO\n")
        for name, description in topic.see_also:
            output.write(f"       mngr help {name} - {description}\n")
        output.write("\n")

    return output.getvalue()


# =============================================================================
# Command resolution helpers
# =============================================================================


def _resolve_command_chain(
    root_group: click.Group,
    parent_ctx: click.Context,
    parts: tuple[str, ...],
) -> list[click.Command] | None:
    """Resolve a chain of command names into a list of click.Command objects.

    Walks through group hierarchies to support subcommands.
    For example, ("snapshot", "create") resolves to [snapshot_group, create_cmd].
    Returns None if any part fails to resolve or if an intermediate command is not a group.
    """
    if not parts:
        return None

    commands: list[click.Command] = []
    current_group = root_group
    current_ctx = parent_ctx

    for i, part in enumerate(parts):
        cmd = current_group.get_command(current_ctx, part)
        if cmd is None:
            return None
        commands.append(cmd)

        if i < len(parts) - 1:
            if not isinstance(cmd, click.Group):
                return None
            current_group = cmd
            current_ctx = click.Context(cmd, info_name=cmd.name, parent=current_ctx)

    return commands


def _get_config_from_ctx(ctx: click.Context) -> MngrConfig | None:
    """Extract MngrConfig from a click context, if available."""
    root_ctx = ctx.find_root()
    if hasattr(root_ctx, "obj") and root_ctx.obj is not None and hasattr(root_ctx.obj, "config"):
        config: MngrConfig = root_ctx.obj.config
        return config
    return None


# =============================================================================
# Help display functions
# =============================================================================


def _show_command_help(
    ctx: click.Context,
    commands: list[click.Command],
) -> None:
    """Show help for a resolved command chain, equivalent to ``--help``."""
    root_ctx = ctx.parent
    assert root_ctx is not None

    # Build context chain: root -> intermediate groups -> target command.
    # This gives _build_help_key the correct chain to produce the right
    # dot-separated key (e.g., "snapshot.create").
    parent_ctx = root_ctx
    for cmd in commands[:-1]:
        parent_ctx = click.Context(cmd, info_name=cmd.name, parent=parent_ctx)

    target_cmd = commands[-1]
    target_ctx = click.Context(target_cmd, info_name=target_cmd.name, parent=parent_ctx)

    help_key = ".".join(cmd.name for cmd in commands if cmd.name is not None)
    metadata = get_help_metadata(help_key)

    help_text = format_git_style_help(target_ctx, target_cmd, metadata)
    config = _get_config_from_ctx(ctx)
    run_pager(help_text, config)


def _show_topic_help(ctx: click.Context, topic: TopicHelpPage) -> None:
    """Show a standalone topic help page through the pager."""
    help_text = format_topic_help(topic)
    config = _get_config_from_ctx(ctx)
    run_pager(help_text, config)


def _show_help_overview(ctx: click.Context) -> None:
    """Show an overview of all available commands and topics."""
    output = StringIO()

    output.write("NAME\n")
    output.write("       mngr help - Show help for a command or topic\n")
    output.write("\n")

    output.write("SYNOPSIS\n")
    output.write("       mngr help [<command> | <topic>]\n")
    output.write("\n")

    output.write("DESCRIPTION\n")
    output.write("       Show help for a mngr command or topic. Without arguments, lists\n")
    output.write("       all available commands and help topics.\n")
    output.write("\n")
    output.write("       For commands, 'mngr help <command>' is equivalent to\n")
    output.write("       'mngr <command> --help'. Command aliases are supported.\n")
    output.write("\n")

    all_metadata = get_all_help_metadata()
    if all_metadata:
        output.write("COMMANDS\n")
        for key, meta in sorted(all_metadata.items()):
            name_str = key.replace(".", " ")
            if meta.aliases:
                name_str += f", {', '.join(meta.aliases)}"
            output.write(f"       {name_str:<28} {meta.one_line_description}\n")
        output.write("\n")

    all_topics = get_all_topics()
    if all_topics:
        output.write("TOPICS\n")
        for key, topic in sorted(all_topics.items()):
            name_str = key
            if topic.aliases:
                name_str += f", {', '.join(topic.aliases)}"
            output.write(f"       {name_str:<28} {topic.one_line_description}\n")
        output.write("\n")

    config = _get_config_from_ctx(ctx)
    run_pager(output.getvalue(), config)


# =============================================================================
# Click command
# =============================================================================


@click.command(name="help")
@click.argument("topic", nargs=-1)
@click.pass_context
def help_command(ctx: click.Context, topic: tuple[str, ...]) -> None:
    """Show help for a command or topic."""
    if not topic:
        _show_help_overview(ctx)
        return

    root_ctx = ctx.parent
    assert root_ctx is not None
    root_cmd = root_ctx.command
    if not isinstance(root_cmd, click.Group):
        _show_help_overview(ctx)
        return

    # Try to resolve as a CLI command (supports aliases and subcommands)
    commands = _resolve_command_chain(root_cmd, root_ctx, topic)
    if commands is not None:
        _show_command_help(ctx, commands)
        return

    # Try as a standalone topic page
    topic_page = get_topic(topic[0])
    if topic_page is not None:
        _show_topic_help(ctx, topic_page)
        return

    sys.stderr.write(f"No help found for '{' '.join(topic)}'.\n")
    sys.stderr.write("Run 'mngr help' for a list of commands and topics.\n")
    ctx.exit(1)


# === Help metadata for the help command itself ===

CommandHelpMetadata(
    key="help",
    one_line_description="Show help for a command or topic",
    synopsis="mngr help [<command> | <topic>]",
    description="""Show help for a mngr command or topic. Without arguments, lists all
available commands and help topics.

For commands, 'mngr help <command>' is equivalent to 'mngr <command> --help'.
Command aliases are supported (e.g., 'mngr help c' shows help for 'create').

For subcommands, specify the full command path (e.g., 'mngr help snapshot create').

Help topics provide documentation on concepts that span multiple commands,
such as agent address format.""",
    additional_sections=(
        (
            "Available Topics",
            "| Topic | Aliases | Description |\n"
            "| ----- | ------- | ----------- |\n"
            "| `address` | `addr` | Agent address syntax for targeting agents and hosts |",
        ),
    ),
    examples=(
        ("Show help for the create command", "mngr help create"),
        ("Show help using a command alias", "mngr help c"),
        ("Show help for a subcommand", "mngr help snapshot create"),
        ("Show the address format topic", "mngr help address"),
        ("List all commands and topics", "mngr help"),
    ),
).register()

add_pager_help_option(help_command)


# =============================================================================
# Topic page definitions
# =============================================================================

TopicHelpPage(
    key="address",
    one_line_description="Agent address syntax for targeting agents and hosts",
    aliases=("addr",),
    content="""\
Many mngr commands accept an agent address to specify which agent (and
optionally which host and provider) to target. The address format is:

  [NAME][@[HOST][.PROVIDER]]

All parts are optional:

  NAME                  Agent name only (searches all hosts; local in create)
  NAME@HOST             Agent on a specific existing host
  NAME@HOST.PROVIDER    Agent on a specific host with provider disambiguation
  NAME@.PROVIDER        Agent on a new host (auto-generated host name)
  @HOST                 Auto-named agent on an existing host
  @HOST.PROVIDER        Auto-named agent on an existing host with provider
  @.PROVIDER            Auto-named agent on a new auto-named host

COMPONENTS

  NAME
      The agent name. Must be a valid identifier (lowercase letters, digits,
      and hyphens). If omitted, a name is auto-generated. Without a host
      component, commands that target existing agents search across all
      hosts and providers. In 'mngr create', it defaults to the local host.

  HOST
      The host name. Refers to an existing host unless --new-host is specified.
      If omitted with a provider (e.g., @.modal), a new host with an
      auto-generated name is created.

  PROVIDER
      The provider backend name (e.g., local, docker, modal). Used to
      disambiguate when multiple providers have hosts with the same name,
      or to specify which provider should create a new host.

COMMANDS THAT ACCEPT ADDRESSES

  mngr create   Primary address argument for creating agents
  mngr connect  Agent identifier (supports @HOST.PROVIDER disambiguation)
  mngr destroy  Agent identifier(s)
  mngr exec     Agent identifier(s)
  mngr start    Agent identifier(s)
  mngr stop     Agent identifier(s)
  mngr list     --addrs flag outputs addresses for listed agents

EXAMPLES

  Create an agent locally:
      $ mngr create my-agent

  Create an agent in a new Docker container:
      $ mngr create my-agent@.docker

  Create an agent on an existing Modal host:
      $ mngr create my-agent@my-host.modal

  Create a new named host on Modal:
      $ mngr create my-agent@my-host.modal --new-host

  Connect to an agent, disambiguating by provider:
      $ mngr connect my-agent@my-host.docker

  Destroy an agent on a specific host:
      $ mngr destroy my-agent@my-host\
""",
    see_also=(
        ("create", "Create and run an agent"),
        ("connect", "Connect to an existing agent"),
    ),
).register()
