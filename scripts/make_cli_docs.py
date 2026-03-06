#!/usr/bin/env python3
"""Generate markdown documentation for mng CLI commands and the PyPI README.

Usage:
    uv run python scripts/make_cli_docs.py

This script generates markdown documentation for all CLI commands
and writes them to libs/mng/docs/commands/. It preserves option
groups defined via click_option_group in the generated markdown.

It also generates libs/mng/README.md from the top-level README.md
by converting local relative paths to GitHub URLs (for PyPI rendering).

All content comes from two sources:
- Click command introspection (usage line, options, arguments)
- CommandHelpMetadata (description, synopsis, examples, see also, etc.)
"""

import re
from pathlib import Path

import click
from click_option_group import GroupedOption

from imbue.mng.cli.common_opts import COMMON_OPTIONS_GROUP_NAME
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import get_help_metadata
from imbue.mng.main import BUILTIN_COMMANDS
from imbue.mng.main import PLUGIN_COMMANDS
from imbue.mng.main import cli

# Commands categorized by their documentation location
PRIMARY_COMMANDS = {
    "connect",
    "create",
    "destroy",
    "exec",
    "list",
    "pair",
    "pull",
    "push",
    "rename",
    "start",
    "stop",
}
SECONDARY_COMMANDS = {
    "ask",
    "cleanup",
    "config",
    "gc",
    "limit",
    "events",
    "message",
    "provision",
    "plugin",
    "snapshot",
}
ALIAS_COMMANDS = {
    "clone",
    "migrate",
}


def fix_sentinel_defaults(content: str) -> str:
    """Replace Click's internal Sentinel.UNSET with user-friendly text."""
    return content.replace("`Sentinel.UNSET`", "None")


def _escape_markdown_table(text: str) -> str:
    """Escape characters that would break markdown table formatting."""
    return text.replace("|", "&#x7C;")


def _format_option_names(option: click.Option) -> str:
    """Format option names for display (e.g., '-n', '--name')."""
    names = []
    for opt in option.opts:
        names.append(f"`{opt}`")
    for opt in option.secondary_opts:
        names.append(f"`{opt}`")
    return ", ".join(names)


def _format_option_type(option: click.Option) -> str:
    """Format option type for display."""
    if option.is_flag:
        return "boolean"
    if option.type is not None:
        type_name = option.type.name.lower()
        if isinstance(option.type, click.Choice):
            choices = " &#x7C; ".join(f"`{c}`" for c in option.type.choices)
            return f"choice ({choices})"
        return type_name
    return "text"


def _format_option_default(option: click.Option) -> str:
    """Format option default value for display."""
    if option.default is None:
        return "None"
    if isinstance(option.default, bool):
        return f"`{option.default}`"
    if isinstance(option.default, str):
        if option.default == "":
            return "``"
        return f"`{option.default}`"
    if isinstance(option.default, (int, float)):
        return f"`{option.default}`"
    return f"`{option.default}`"


def _collect_options_by_group(
    command: click.Command,
) -> dict[str | None, list[click.Option]]:
    """Collect command options organized by their option group."""
    options_by_group: dict[str | None, list[click.Option]] = {}

    for param in command.params:
        if not isinstance(param, click.Option):
            continue

        if isinstance(param, GroupedOption):
            group_name = param.group.name
        else:
            group_name = None

        if group_name not in options_by_group:
            options_by_group[group_name] = []
        options_by_group[group_name].append(param)

    return options_by_group


def _order_option_groups(
    options_by_group: dict[str | None, list[click.Option]],
) -> list[str | None]:
    """Order option groups: named groups first, Common last, ungrouped at the end."""
    group_names = list(options_by_group.keys())
    ordered: list[str | None] = []

    # First: named groups (except Common)
    for name in group_names:
        if name is not None and name != COMMON_OPTIONS_GROUP_NAME:
            ordered.append(name)

    # Then: Common group
    if COMMON_OPTIONS_GROUP_NAME in group_names:
        ordered.append(COMMON_OPTIONS_GROUP_NAME)

    # Finally: ungrouped options (None)
    if None in group_names:
        ordered.append(None)

    return ordered


def _generate_options_table(options: list[click.Option]) -> str:
    """Generate a markdown table for a list of options."""
    lines = [
        "| Name | Type | Description | Default |",
        "| ---- | ---- | ----------- | ------- |",
    ]

    for option in options:
        if option.hidden:
            continue

        names = _format_option_names(option)
        opt_type = _format_option_type(option)
        description = _escape_markdown_table(option.help or "")
        default = _format_option_default(option)

        lines.append(f"| {names} | {opt_type} | {description} | {default} |")

    return "\n".join(lines)


def generate_grouped_options_markdown(
    command: click.Command,
    group_intros: dict[str, str] | None = None,
) -> str:
    """Generate markdown for options organized by groups."""
    options_by_group = _collect_options_by_group(command)
    ordered_groups = _order_option_groups(options_by_group)

    if group_intros is None:
        group_intros = {}

    lines: list[str] = []

    for group_name in ordered_groups:
        options = options_by_group[group_name]
        if not options:
            continue

        # Filter out hidden options
        visible_options = [o for o in options if not o.hidden]
        if not visible_options:
            continue

        # Add group heading (use ## for top-level sections)
        if group_name is not None:
            lines.append(f"## {group_name}")
        else:
            lines.append("## Other Options")
        lines.append("")

        # Add group intro if provided
        if group_name is not None and group_name in group_intros:
            lines.append(group_intros[group_name])
            lines.append("")

        # Add options table
        lines.append(_generate_options_table(visible_options))
        lines.append("")

    return "\n".join(lines)


def generate_arguments_section(command: click.Command, command_name: str) -> str:
    """Generate markdown for the Arguments section."""
    # Check if metadata provides a custom arguments description
    metadata = get_help_metadata(command_name)
    if metadata is not None and metadata.arguments_description is not None:
        return f"## Arguments\n\n{metadata.arguments_description}\n"

    # Collect click.Argument params
    arguments = [p for p in command.params if isinstance(p, click.Argument)]
    if not arguments:
        return ""

    lines = ["## Arguments", ""]

    for arg in arguments:
        # Use human_readable_name (returns metavar if set) for user-facing display
        arg_name = arg.human_readable_name
        if arg_name is None:
            raise ValueError(f"Argument {arg.name!r} is missing a metavar; add metavar= to the click.argument() call")
        arg_name = arg_name.upper()
        description = _infer_argument_description(arg)
        lines.append(f"- `{arg_name}`: {description}")

    lines.append("")
    return "\n".join(lines)


def _infer_argument_description(arg: click.Argument) -> str:
    """Infer a description for an argument based on its properties."""
    name = (arg.name or "arg").removesuffix("_pos")

    # Common argument patterns
    if "name" in name.lower():
        if arg.required:
            return "Name for the resource"
        return "Name for the resource (auto-generated if not provided)"
    if "type" in name.lower():
        return "Type to use"
    if "args" in name.lower():
        return "Additional arguments passed through"

    # Generic fallback
    if arg.required:
        return f"The {name.replace('_', ' ')}"
    return f"The {name.replace('_', ' ')} (optional)"


# ---------------------------------------------------------------------------
# Click usage extraction
# ---------------------------------------------------------------------------


def _format_usage_line(command: click.Command, prog_name: str) -> str:
    """Get the click-generated usage line for a command."""
    ctx = click.Context(command, info_name=prog_name)
    pieces = command.collect_usage_pieces(ctx)
    if pieces:
        return f"{prog_name} {' '.join(pieces)}"
    return prog_name


def _format_usage_block(command: click.Command, prog_name: str) -> str:
    """Generate the **Usage:** markdown block for a command."""
    usage_line = _format_usage_line(command, prog_name)
    return f"**Usage:**\n\n```text\n{usage_line}\n```"


# ---------------------------------------------------------------------------
# Metadata formatting
# ---------------------------------------------------------------------------


def _format_description_block(metadata: CommandHelpMetadata) -> str:
    """Format a description + alias block from metadata for markdown docs."""
    lines: list[str] = []
    for paragraph in metadata.full_description.strip().split("\n\n"):
        lines.append(paragraph.strip())
        lines.append("")

    if metadata.aliases:
        alias_str = ", ".join(metadata.aliases)
        lines.append(f"Alias: {alias_str}")
        lines.append("")

    return "\n".join(lines)


def format_synopsis(metadata: CommandHelpMetadata) -> str:
    """Format synopsis section from metadata."""
    if not metadata.synopsis:
        return ""

    lines = ["", "**Synopsis:**", "", "```text"]
    for line in metadata.synopsis.strip().split("\n"):
        lines.append(line)
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def format_examples(metadata: CommandHelpMetadata) -> str:
    """Format examples section from metadata."""
    if not metadata.examples:
        return ""

    lines = ["", "## Examples", ""]
    for description, command in metadata.examples:
        lines.append(f"**{description}**")
        lines.append("")
        lines.append("```bash")
        lines.append(f"$ {command}")
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def format_additional_sections(metadata: CommandHelpMetadata) -> str:
    """Format additional documentation sections from metadata."""
    sections = []

    if metadata.additional_sections:
        for title, content in metadata.additional_sections:
            if title == "See Also":
                continue
            sections.append(f"\n## {title}\n")
            sections.append(content)
            sections.append("")

    return "\n".join(sections)


def get_command_category(command_name: str) -> str | None:
    """Get the category (primary/secondary/aliases) for a command."""
    if command_name in PRIMARY_COMMANDS:
        return "primary"
    elif command_name in SECONDARY_COMMANDS:
        return "secondary"
    elif command_name in ALIAS_COMMANDS:
        return "aliases"
    return None


def get_relative_link(from_command: str, to_command: str) -> str:
    """Get the relative markdown link path from one command's doc to another."""
    from_category = get_command_category(from_command)
    to_category = get_command_category(to_command)

    if to_category is None:
        return f"mng {to_command}"

    if from_category == to_category:
        return f"./{to_command}.md"
    else:
        return f"../{to_category}/{to_command}.md"


def format_see_also_section(command_name: str, metadata: CommandHelpMetadata) -> str:
    """Format the See Also section from metadata with markdown links."""
    if not metadata.see_also:
        return ""

    lines = ["", "## See Also", ""]
    for ref_command, description in metadata.see_also:
        link = get_relative_link(command_name, ref_command)
        lines.append(f"- [mng {ref_command}]({link}) - {description}")

    lines.append("")
    return "\n".join(lines)


def get_output_dir(command_name: str, base_dir: Path) -> Path | None:
    """Determine the output directory for a command based on its category."""
    category = get_command_category(command_name)
    if category is not None:
        return base_dir / category
    return None


# ---------------------------------------------------------------------------
# Subcommand docs
# ---------------------------------------------------------------------------


def generate_subcommand_docs(command: click.Group, prog_name: str, parent_key: str) -> str:
    """Generate documentation for all subcommands with grouped options."""
    if not hasattr(command, "commands") or not command.commands:
        return ""

    lines: list[str] = []

    for subcmd_name, subcmd in command.commands.items():
        subcmd_key = f"{parent_key}.{subcmd_name}"
        subcmd_prog = f"{prog_name} {subcmd_name}"
        subcmd_metadata = get_help_metadata(subcmd_key)

        # Title (## level for subcommands)
        lines.append(f"## {subcmd_prog}")
        lines.append("")

        # Description from metadata
        if subcmd_metadata is not None and subcmd_metadata.full_description:
            lines.append(_format_description_block(subcmd_metadata))

        # Usage
        lines.append(_format_usage_block(subcmd, subcmd_prog))

        # Options
        lines.append("**Options:**")
        lines.append("")
        lines.append(generate_grouped_options_markdown(subcmd))

        # Examples from metadata
        if subcmd_metadata is not None and subcmd_metadata.examples:
            lines.append(format_examples(subcmd_metadata))

        # Recurse for nested subcommands
        if isinstance(subcmd, click.Group) and subcmd.commands:
            nested_docs = generate_subcommand_docs(subcmd, subcmd_prog, parent_key=subcmd_key)
            if nested_docs:
                lines.append(nested_docs)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level command doc generation
# ---------------------------------------------------------------------------


def generate_command_doc(command_name: str, base_dir: Path) -> None:
    """Generate markdown documentation for a single command."""
    output_dir = get_output_dir(command_name, base_dir)
    if output_dir is None:
        print(f"Skipping: {command_name} (not in PRIMARY_COMMANDS or SECONDARY_COMMANDS)")
        return

    cmd = cli.commands.get(command_name)
    if cmd is None:
        print(f"Warning: Command '{command_name}' not found")
        return

    prog_name = f"mng {command_name}"
    metadata = get_help_metadata(command_name)

    # Build content parts
    content_parts: list[str] = []

    # Title
    content_parts.append(f"# {prog_name}")

    # Synopsis from metadata
    if metadata is not None:
        synopsis = format_synopsis(metadata)
        if synopsis:
            content_parts.append(synopsis)

    # Description from metadata
    if metadata is not None:
        content_parts.append(_format_description_block(metadata))

    # Usage from click
    content_parts.append(_format_usage_block(cmd, prog_name))

    # Arguments section
    arguments_section = generate_arguments_section(cmd, command_name)
    if arguments_section:
        content_parts.append(arguments_section)

    # Group intros from metadata
    group_intros: dict[str, str] = {}
    if metadata is not None and metadata.group_intros:
        group_intros = dict(metadata.group_intros)

    # Options
    content_parts.append("**Options:**")
    content_parts.append("")
    content_parts.append(generate_grouped_options_markdown(cmd, group_intros))

    # Subcommand documentation
    if isinstance(cmd, click.Group) and cmd.commands:
        subcommand_docs = generate_subcommand_docs(cmd, prog_name, parent_key=command_name)
        if subcommand_docs:
            content_parts.append(subcommand_docs)

    # Combine all parts
    content = "\n".join(content_parts)
    content = fix_sentinel_defaults(content)

    # Additional sections, see also, examples from metadata
    if metadata is not None:
        content += format_additional_sections(metadata)
        content += format_see_also_section(command_name, metadata)
        content += format_examples(metadata)

    # Add generation comment at the top
    generation_comment = (
        "<!-- This file is auto-generated. Do not edit directly. -->\n"
        "<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->\n\n"
    )
    content = generation_comment + content

    # Write to file (only if changed)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{command_name}.md"
    existing_content = output_file.read_text() if output_file.exists() else None
    if content != existing_content:
        output_file.write_text(content)
        print(f"Updated: {output_file}")


def generate_alias_doc(command_name: str, base_dir: Path) -> None:
    """Generate markdown documentation for an alias command.

    Alias commands (like clone, migrate) use UNPROCESSED args and delegate to
    other commands. Their docs are built entirely from CommandHelpMetadata.
    """
    output_dir = get_output_dir(command_name, base_dir)
    if output_dir is None:
        print(f"Skipping: {command_name} (not in ALIAS_COMMANDS)")
        return

    metadata = get_help_metadata(command_name)
    if metadata is None:
        print(f"Warning: No help metadata for alias command '{command_name}'")
        return

    content_parts: list[str] = []

    # Title
    content_parts.append(f"# mng {command_name}")

    # Synopsis
    synopsis = format_synopsis(metadata)
    if synopsis:
        content_parts.append(synopsis)

    # Description
    content_parts.append(metadata.full_description)
    content_parts.append("")

    # Additional sections
    additional = format_additional_sections(metadata)
    if additional:
        content_parts.append(additional)

    # See Also
    see_also = format_see_also_section(command_name, metadata)
    if see_also:
        content_parts.append(see_also)

    # Examples
    examples = format_examples(metadata)
    if examples:
        content_parts.append(examples)

    content = "\n".join(content_parts)

    # Add generation comment at the top
    generation_comment = (
        "<!-- This file is auto-generated. Do not edit directly. -->\n"
        "<!-- To modify, edit the command's help metadata and run: uv run python scripts/make_cli_docs.py -->\n\n"
    )
    content = generation_comment + content

    # Write to file (only if changed)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{command_name}.md"
    existing_content = output_file.read_text() if output_file.exists() else None
    if content != existing_content:
        output_file.write_text(content)
        print(f"Updated: {output_file}")


GITHUB_BASE_URL = "https://github.com/imbue-ai/mng/blob/main/"

# Matches markdown link targets: ](path) — but not absolute URLs, anchors, or mailto
_RELATIVE_LINK_RE = re.compile(r"\]\((?!https?://|#|mailto:)([^)]+)\)")


def _local_path_to_github_url(match: re.Match[str]) -> str:
    """Convert a relative markdown link target to a GitHub URL."""
    path = match.group(1)
    return f"]({GITHUB_BASE_URL}{path})"


def generate_pypi_readme(repo_root: Path) -> None:
    """Generate libs/mng/README.md from the top-level README.md.

    Reads the top-level README (which uses local relative paths) and writes
    a version with GitHub absolute URLs for PyPI rendering.
    """
    source = repo_root / "README.md"
    dest = repo_root / "libs" / "mng" / "README.md"

    content = source.read_text()

    # Convert local relative paths to GitHub URLs
    content = _RELATIVE_LINK_RE.sub(_local_path_to_github_url, content)

    # Add autogen comment at the top
    generation_comment = (
        "<!-- This file is auto-generated. Do not edit directly. -->\n"
        "<!-- This is a copy of the top-level README.md, but with local paths replaced by GitHub links. -->\n"
        "<!-- To modify, edit README.md in the repo root and run: uv run python scripts/make_cli_docs.py -->\n\n"
    )
    content = generation_comment + content

    # Write only if changed
    existing_content = dest.read_text() if dest.exists() else None
    if content != existing_content:
        dest.write_text(content)
        print(f"Updated: {dest}")


def main() -> None:
    repo_root = Path(__file__).parent.parent

    # Generate PyPI README from top-level README
    generate_pypi_readme(repo_root)

    # Generate CLI command docs
    base_dir = repo_root / "libs" / "mng" / "docs" / "commands"

    for cmd in BUILTIN_COMMANDS + PLUGIN_COMMANDS:
        if cmd.name is not None:
            generate_command_doc(cmd.name, base_dir)

    # Generate docs for alias commands
    for command_name in sorted(ALIAS_COMMANDS):
        generate_alias_doc(command_name, base_dir)


if __name__ == "__main__":
    main()
