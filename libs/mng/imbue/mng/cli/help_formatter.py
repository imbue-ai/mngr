import os
import shutil
import subprocess
import sys
import textwrap
from io import StringIO
from typing import Any
from typing import cast

import click
from click_option_group import GroupedOption
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mng.cli.common_opts import COMMON_OPTIONS_GROUP_NAME
from imbue.mng.cli.common_opts import find_option_group
from imbue.mng.config.data_types import MngConfig
from imbue.mng.utils.interactive_subprocess import popen_interactive_subprocess


class CommandHelpMetadata(FrozenModel):
    """Metadata for git-style help formatting.

    This contains the extra information needed to produce git-style help
    that isn't available from click's standard help machinery.
    """

    key: str = Field(description="Dot-separated registry key (e.g., 'create', 'snapshot.create')")
    one_line_description: str = Field(description="Brief one-line description of the command")
    synopsis: str = Field(description="Usage synopsis showing command patterns")
    description: str = Field(description="Extended description (paragraphs after the one-line summary)")
    aliases: tuple[str, ...] = Field(default=(), description="Command aliases (e.g., ('c',) for 'create')")
    arguments_description: str | None = Field(
        default=None,
        description="Description of positional arguments (markdown). If None, auto-generated from click arguments.",
    )
    examples: tuple[tuple[str, str], ...] = Field(
        default=(), description="List of (description, command) example tuples"
    )
    additional_sections: tuple[tuple[str, str], ...] = Field(
        default=(),
        description="Additional documentation sections as (title, markdown_content) tuples",
    )
    group_intros: tuple[tuple[str, str], ...] = Field(
        default=(),
        description="Introductory text for option groups as (group_name, markdown_content) tuples. "
        "The intro text appears before the options table for that group.",
    )
    see_also: tuple[tuple[str, str], ...] = Field(
        default=(),
        description="See Also references as (command_name, description) tuples. "
        "Command name is just the subcommand (e.g., 'create' not 'mng create').",
    )

    @property
    def name(self) -> str:
        """Display name derived from key (e.g., key='snapshot.create' -> 'mng snapshot create')."""
        return "mng " + self.key.replace(".", " ")

    @property
    def full_description(self) -> str:
        """One-line description (with period) + extended description, separated by a blank line."""
        one_line = self.one_line_description
        if not one_line.endswith("."):
            one_line += "."
        if self.description:
            return one_line + "\n\n" + self.description
        return one_line

    def register(self) -> None:
        """Register this metadata in the global help registry, keyed by self.key."""
        _help_metadata_registry[self.key] = self


# Registry of help metadata for commands that have been configured
_help_metadata_registry: dict[str, CommandHelpMetadata] = {}


def get_help_metadata(command_name: str) -> CommandHelpMetadata | None:
    """Get help metadata for a command, if registered."""
    return _help_metadata_registry.get(command_name)


def _build_help_key(ctx: click.Context) -> str | None:
    """Build a dot-separated registry key from the context chain using canonical names.

    Walks from the current context up to (but not including) the root CLI group.
    Uses ``ctx.command.name`` (the canonical name) rather than ``ctx.info_name``
    (which reflects whichever alias was used at invocation).  This means
    ``mng snap create --help`` produces key ``"snapshot.create"`` -- the same key
    used by the registration call -- so aliases resolve correctly with no
    duplicate registry entries.

    Note: this function requires the full context chain from the root CLI group.
    Tests that invoke a subgroup directly (e.g., ``cli_runner.invoke(snapshot, ...)``)
    will produce incorrect keys because the subgroup becomes the root.  Always use
    ``cli_runner.invoke(cli, ["snapshot", ...])`` when testing help output.
    """
    parts: list[str] = []
    current: click.Context | None = ctx
    while current is not None and current.parent is not None:
        name = current.command.name if current.command.name is not None else current.info_name
        if name is not None:
            parts.append(name)
        current = current.parent
    if not parts:
        return None
    parts.reverse()
    return ".".join(parts)


def _resolve_help_metadata(ctx: click.Context) -> CommandHelpMetadata | None:
    """Get help metadata for a command from its click context.

    Uses ``_build_help_key`` to construct a canonical dot-separated key
    (e.g., ``"snapshot.create"``) and performs a single registry lookup --
    no fallback, no alias variants.
    """
    key = _build_help_key(ctx)
    if key is None:
        return None
    return _help_metadata_registry.get(key)


def get_all_help_metadata() -> dict[str, CommandHelpMetadata]:
    """Return a copy of the full help metadata registry."""
    return dict(_help_metadata_registry)


def is_interactive_terminal() -> bool:
    """Check if stdout is an interactive terminal.

    Returns False if stdout is not available (e.g., in some test environments).
    """
    try:
        return sys.stdout.isatty()
    except (ValueError, AttributeError):
        # Handle cases where stdout is uninitialized (e.g., xdist workers)
        return False


@pure
def get_terminal_width() -> int:
    """Get the terminal width, defaulting to 80 if not detectable."""
    terminal_size = shutil.get_terminal_size()
    return terminal_size.columns


@pure
def get_pager_command(config: MngConfig | None) -> str:
    """Determine the pager command to use.

    Priority:
    1. Config pager setting
    2. PAGER environment variable
    3. Default to "less"
    """
    if config is not None and config.pager is not None:
        return config.pager

    return os.environ.get("PAGER", "less")


def _write_to_stdout(text: str) -> None:
    """Write text to stdout, followed by a newline."""
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def run_pager(text: str, config: MngConfig | None) -> None:
    """Display text through a pager if in an interactive terminal.

    If not interactive, just prints the text directly.
    """
    if not is_interactive_terminal():
        _write_to_stdout(text)
        return

    _run_pager_with_subprocess(text, config)


def _run_pager_with_subprocess(text: str, config: MngConfig | None) -> None:
    """Display text through a pager subprocess.

    Falls back to writing directly to stdout if the pager fails.
    """
    pager_cmd = get_pager_command(config)

    # Set up environment for less to handle ANSI codes and not require explicit quit
    env = os.environ.copy()
    if "less" in pager_cmd.lower():
        # -R: output raw control characters (for ANSI)
        # -F: quit if output fits on one screen
        # -X: don't clear screen on exit
        env["LESS"] = env.get("LESS", "") + " -RFX"

    try:
        process = popen_interactive_subprocess(
            pager_cmd,
            shell=True,
            stdin=subprocess.PIPE,
            env=env,
        )
        process.communicate(input=text.encode("utf-8"))
    except (OSError, subprocess.SubprocessError):
        # If pager fails, fall back to direct output
        _write_to_stdout(text)


@pure
def _wrap_text(text: str, width: int, indent: str, subsequent_indent: str | None) -> str:
    """Wrap text with proper indentation."""
    if subsequent_indent is None:
        subsequent_indent = indent
    wrapper = textwrap.TextWrapper(
        width=width,
        initial_indent=indent,
        subsequent_indent=subsequent_indent,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return wrapper.fill(text)


@pure
def _format_section_title(title: str) -> str:
    """Format a section title in man-page style (uppercase)."""
    return title.upper()


def format_git_style_help(
    ctx: click.Context,
    command: click.Command,
    metadata: CommandHelpMetadata | None,
) -> str:
    """Format help output in git's man-page style.

    Produces output with sections:
    - NAME: command name and one-line description
    - SYNOPSIS: usage line
    - DESCRIPTION: detailed description
    - OPTIONS: all options organized by groups
    - EXAMPLES: usage examples (if provided)
    """
    output = StringIO()
    width = get_terminal_width()

    # If we have metadata, use git-style formatting
    if metadata is not None:
        _write_git_style_help(output, ctx, command, metadata, width)
    else:
        # Fall back to standard click formatting
        output.write(command.get_help(ctx))

    return output.getvalue()


def _write_git_style_help(
    output: StringIO,
    ctx: click.Context,
    command: click.Command,
    metadata: CommandHelpMetadata,
    width: int,
) -> None:
    """Write git-style help to the output buffer."""
    # NAME section
    output.write(f"{_format_section_title('Name')}\n")
    output.write(f"       {metadata.name} - {metadata.one_line_description}\n")
    output.write("\n")

    # SYNOPSIS section
    output.write(f"{_format_section_title('Synopsis')}\n")
    for line in metadata.synopsis.strip().split("\n"):
        output.write(f"       {line}\n")
    output.write("\n")

    # DESCRIPTION section
    output.write(f"{_format_section_title('Description')}\n")
    for paragraph in metadata.full_description.strip().split("\n\n"):
        wrapped = _wrap_text(paragraph.strip(), width - 7, "       ", None)
        output.write(f"{wrapped}\n\n")

    # OPTIONS section
    output.write(f"{_format_section_title('Options')}\n")
    _write_options_section(output, ctx, command, width)

    # ADDITIONAL SECTIONS (if provided)
    if metadata.additional_sections:
        for title, content in metadata.additional_sections:
            output.write(f"{_format_section_title(title)}\n")
            for line in content.strip().split("\n"):
                output.write(f"       {line}\n")
            output.write("\n")

    # SEE ALSO section (if provided)
    if metadata.see_also:
        output.write(f"{_format_section_title('See Also')}\n")
        for command_name, description in metadata.see_also:
            output.write(f"       mng {command_name} --help - {description}\n")
        output.write("\n")

    # EXAMPLES section (if provided)
    if metadata.examples:
        output.write(f"{_format_section_title('Examples')}\n")
        for description, example in metadata.examples:
            output.write(f"       {description}\n")
            output.write(f"           $ {example}\n\n")


def _write_options_section(
    output: StringIO,
    ctx: click.Context,
    command: click.Command,
    width: int,
) -> None:
    """Write the OPTIONS section with option groups."""
    # Collect options by group
    options_by_group: dict[str | None, list[click.Option]] = {}

    for param in command.params:
        if not isinstance(param, click.Option):
            continue

        # Check if this is a grouped option and add to appropriate group
        if isinstance(param, GroupedOption):
            group_name = param.group.name
            options_by_group.setdefault(group_name, []).append(param)
        else:
            # Non-grouped option goes in the default group
            options_by_group.setdefault(None, []).append(param)

    # Write options by group, with Common group last
    # Build ordered list of group names: other groups first, then Common, then ungrouped (None)
    group_names = list(options_by_group.keys())
    ordered_group_names: list[str | None] = []

    # First add all groups except Common and None (ungrouped)
    for name in group_names:
        if name is not None and name != COMMON_OPTIONS_GROUP_NAME:
            ordered_group_names.append(name)

    # Then add Common group if it exists
    if COMMON_OPTIONS_GROUP_NAME in group_names:
        ordered_group_names.append(COMMON_OPTIONS_GROUP_NAME)

    # Finally add ungrouped options (None) if any exist
    if None in group_names:
        ordered_group_names.append(None)

    for group_name in ordered_group_names:
        options = options_by_group[group_name]
        visible_options = [o for o in options if not o.hidden]
        if not visible_options:
            continue
        # Display "Ungrouped" for options without a group
        display_name = group_name if group_name is not None else "Ungrouped"
        output.write(f"\n   {display_name}\n")

        for option in visible_options:
            _write_option(output, ctx, option, width)


def _write_option(
    output: StringIO,
    ctx: click.Context,
    option: click.Option,
    width: int,
) -> None:
    """Write a single option in man-page style."""
    # Build the option string (e.g., "-v, --verbose")
    opt_parts = []
    for opt in option.opts:
        opt_parts.append(opt)
    for opt in option.secondary_opts:
        opt_parts.append(opt)

    opt_str = ", ".join(opt_parts)

    # Add metavar if applicable
    if option.metavar:
        opt_str += f" {option.metavar}"
    elif option.type and not option.is_flag:
        metavar = option.type.name.upper()
        if metavar != "TEXT":
            opt_str += f" {metavar}"
    else:
        pass

    # Write the option name
    output.write(f"       {opt_str}\n")

    # Write the help text indented
    if option.help:
        help_text = option.help
        # Append default value if show_default is True
        if option.show_default:
            default = option.default
            if default is not None and not option.is_flag:
                help_text += f" [default: {default}]"
        wrapped = _wrap_text(help_text, width - 11, "           ", None)
        output.write(f"{wrapped}\n")
    output.write("\n")


class GitStyleHelpMixin:
    """Mixin class to add git-style help formatting to click commands.

    Add this mixin to a command class to enable git-style help output
    with pager support.
    """

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        """Format help using git-style formatting if metadata is available."""
        if ctx.info_name is None:
            # Fall back to standard formatting - cast self to click.Command for type checker
            parent_format_help = getattr(super(), "format_help", None)
            if parent_format_help is not None:
                parent_format_help(ctx, formatter)
            return

        metadata = _resolve_help_metadata(ctx)
        # Cast self to click.Command since this mixin is only used with Command subclasses
        command = cast(click.Command, self)
        help_text = format_git_style_help(ctx, command, metadata)

        # Write to formatter's buffer
        formatter.write(help_text)


def show_help_with_pager(
    ctx: click.Context,
    command: click.Command,
    config: MngConfig | None,
) -> None:
    """Show help for a command using a pager if appropriate.

    This is the main entry point for displaying help with pager support.
    """
    metadata = _resolve_help_metadata(ctx)

    help_text = format_git_style_help(ctx, command, metadata)
    run_pager(help_text, config)


def help_option_callback(
    ctx: click.Context,
    param: click.Parameter,
    value: Any,
) -> None:
    """Callback for custom --help option that uses pager.

    This replaces click's default help option callback to add pager support.
    """
    if not value or ctx.resilient_parsing:
        return

    command = ctx.command

    # Try to get config from context for pager settings
    config: MngConfig | None = None
    if hasattr(ctx, "obj") and ctx.obj is not None:
        # ctx.obj might be a MngContext or PluginManager depending on when --help is called
        if hasattr(ctx.obj, "config"):
            config = ctx.obj.config

    show_help_with_pager(ctx, command, config)
    ctx.exit(0)


def add_pager_help_option(command: click.Command) -> click.Command:
    """Replace the default --help option with one that uses a pager.

    The new option is placed in the Common option group (if one exists on the
    command) so it appears alongside other shared options rather than under
    "Ungrouped".

    This modifies the command in-place and returns it for chaining.
    """
    # Remove existing help option
    command.params = [p for p in command.params if not (isinstance(p, click.Option) and p.name == "help")]

    # Add new help option with pager callback, in the Common group if available
    common_group = find_option_group(command, COMMON_OPTIONS_GROUP_NAME)
    if common_group is not None:
        help_option: click.Option = GroupedOption(
            ["-h", "--help"],
            group=common_group,
            is_flag=True,
            expose_value=False,
            is_eager=True,
            callback=help_option_callback,
            help="Show this message and exit.",
        )
    else:
        help_option = click.Option(
            ["-h", "--help"],
            is_flag=True,
            expose_value=False,
            is_eager=True,
            callback=help_option_callback,
            help="Show this message and exit.",
        )
    command.params.append(help_option)

    return command
