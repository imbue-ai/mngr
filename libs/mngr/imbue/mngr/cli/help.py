"""Help command and standalone topic pages for the mngr CLI.

Provides two types of help:
1. Command help: ``mngr help create`` is equivalent to ``mngr create --help``
2. Topic help: ``mngr help address`` shows a standalone documentation page

Both commands and topics support aliases (e.g., ``mngr help c`` for create,
``mngr help addr`` for address).
"""

from io import StringIO

import click
import pluggy
from loguru import logger

from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.help_formatter import format_git_style_help
from imbue.mngr.cli.help_formatter import get_all_help_metadata
from imbue.mngr.cli.help_formatter import get_help_metadata
from imbue.mngr.cli.help_formatter import get_terminal_width
from imbue.mngr.cli.help_formatter import is_interactive_terminal
from imbue.mngr.cli.help_formatter import render_markdown
from imbue.mngr.cli.help_formatter import run_pager
from imbue.mngr.cli.help_topics import get_all_topics
from imbue.mngr.cli.help_topics import get_topic
from imbue.mngr.cli.help_topics import register_topic
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.interfaces.help_topic import TopicHelpPage

# =============================================================================
# Topic registration
# =============================================================================


def load_help_topics_from_plugins(pm: pluggy.PluginManager) -> None:
    """Register all help topics contributed via the ``register_help_topics`` hook.

    Both mngr's built-in topics and external plugins' topics flow through this
    one path: built-in topics come from the ``builtin_help_topics`` module that
    ``create_plugin_manager`` registers as a built-in plugin, and external
    topics come from installed plugins. Called once after the plugin manager has
    loaded (see ``main.py``).

    A topic whose key is already taken is skipped. Built-in topics are
    registered first (their hook is marked ``tryfirst``), so they win on
    collisions and an external plugin cannot override a built-in topic.
    """
    for topic_list in pm.hook.register_help_topics():
        if topic_list is None:
            continue
        for topic in topic_list:
            if not register_topic(topic):
                logger.warning(
                    "Help topic '{}' conflicts with an already-registered topic and was skipped.",
                    topic.key,
                )


# =============================================================================
# Formatting
# =============================================================================


def format_topic_help(topic: TopicHelpPage, *, use_ansi: bool, width: int) -> str:
    """Format a topic help page for terminal display.

    The body (inline or file-backed, markdown either way) is rendered as markdown
    -- via rich when ``use_ansi`` is True, otherwise emitted as raw markdown. For
    a file-backed topic with a known source URL, relative and anchor links are
    rewritten to absolute URLs so they are clickable in the terminal. A SEE ALSO
    section is appended when references are present.
    """
    output = StringIO()
    output.write(render_markdown(topic.load_body(), use_ansi=use_ansi, width=width, link_base=topic.link_base_url()))
    if not output.getvalue().endswith("\n"):
        output.write("\n")

    if topic.see_also:
        output.write("SEE ALSO\n")
        for name, description in topic.see_also:
            # Strip any "#anchor" suffix; anchors are only meaningful for the
            # markdown doc generator, not for a terminal `mngr help <name>`.
            bare_name = name.partition("#")[0]
            output.write(f"       mngr help {bare_name} - {description}\n")
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

    help_text = format_git_style_help(target_ctx, target_cmd, metadata, use_ansi=is_interactive_terminal())
    config = _get_config_from_ctx(ctx)
    run_pager(help_text, config)


def _show_topic_help(ctx: click.Context, topic: TopicHelpPage) -> None:
    """Show a standalone topic help page through the pager."""
    help_text = format_topic_help(topic, use_ansi=is_interactive_terminal(), width=get_terminal_width())
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

    logger.error("No help found for '{}'.", " ".join(topic))
    logger.error("Run 'mngr help' for a list of commands and topics.")
    ctx.exit(1)


# =============================================================================
# Help metadata for the help command itself
# =============================================================================


def build_available_topics_section() -> str:
    """Build the Available Topics section content from the topic registry.

    Uses a bullet-list format that renders well in both terminal (indented
    by the help formatter) and markdown (in generated docs). Returns an empty
    string when no topics are registered yet.
    """
    all_topics = get_all_topics()
    if not all_topics:
        return ""
    lines: list[str] = []
    for key, topic in sorted(all_topics.items()):
        name_str = key
        if topic.aliases:
            name_str += f" ({', '.join(topic.aliases)})"
        lines.append(f"- {name_str} - {topic.one_line_description}")
    return "\n".join(lines)


def register_help_command_metadata() -> None:
    """Build and register the help command's own help metadata.

    Called once after topics are registered (built-in and plugin), since the
    "Available Topics" section lists every registered topic. Unlike the other
    commands -- whose metadata is registered at module import -- the help
    command's metadata is registered here so its topic list reflects the fully
    loaded registry without any post-hoc mutation. (Compare
    ``_update_create_help_with_provider_args`` in ``main.py``, which similarly
    finalizes the create command's help once backends are loaded.)
    """
    available_topics = build_available_topics_section()
    additional_sections = (("Available Topics", available_topics),) if available_topics else ()
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
        additional_sections=additional_sections,
        examples=(
            ("Show help for the create command", "mngr help create"),
            ("Show help using a command alias", "mngr help c"),
            ("Show help for a subcommand", "mngr help snapshot create"),
            ("Show the address format topic", "mngr help address"),
            ("List all commands and topics", "mngr help"),
        ),
    ).register()


add_pager_help_option(help_command)
