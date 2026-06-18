#!/usr/bin/env python3
"""Generate markdown documentation for mngr CLI commands and the PyPI README.

Usage:
    uv run python scripts/make_cli_docs.py            # regenerate the docs in place
    uv run python scripts/make_cli_docs.py --check     # exit non-zero if any doc is stale

This script generates markdown documentation for all CLI commands
and writes them to libs/mngr/docs/commands/. It preserves option
groups defined via click_option_group in the generated markdown.

It also generates libs/mngr/README.md from the top-level README.md
by converting local relative paths to GitHub URLs (for PyPI rendering).

All content comes from two sources:
- Click command introspection (usage line, options, arguments)
- CommandHelpMetadata (description, synopsis, examples, see also, etc.)
"""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

# Force all plugins to load regardless of local config so generated docs
# always reflect every provider (docker, modal, etc.).  Must be set before
# importing main, which triggers plugin-manager creation at import time.
os.environ["MNGR_LOAD_ALL_PLUGINS"] = "1"

import click
from click_option_group import GroupedOption
from pydantic import BaseModel

from imbue.mngr.cli.common_opts import COMMON_OPTIONS_GROUP_NAME
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import get_help_metadata
from imbue.mngr.cli.help_topics import get_topic
from imbue.mngr.main import BUILTIN_COMMANDS
from imbue.mngr.main import PLUGIN_COMMANDS
from imbue.mngr.main import cli
from imbue.mngr_aws.config import AwsProviderConfig
from imbue.mngr_gcp.config import GcpProviderConfig
from imbue.mngr_opencode.plugin import OpenCodeAgentConfig
from imbue.mngr_ovh.config import OvhProviderConfig
from imbue.mngr_pi_coding.plugin import PiCodingAgentConfig
from imbue.mngr_vps.config import VpsProviderConfig
from imbue.mngr_vultr.config import VultrProviderConfig

# Commands categorized by their documentation location
PRIMARY_COMMANDS = {
    "connect",
    "create",
    "destroy",
    "exec",
    "git",
    "list",
    "pair",
    "rename",
    "rsync",
    "start",
    "stop",
}
SECONDARY_COMMANDS = {
    "ask",
    "aws",
    "azure",
    "capture",
    "chat",
    "cleanup",
    "config",
    "event",
    "file",
    "forward",
    "gc",
    "gcp",
    "help",
    "imbue_cloud",
    "kanpan",
    "latchkey",
    "label",
    "limit",
    "message",
    "observe",
    "ovh",
    "plugin",
    "schedule",
    "snapshot",
    "tmr",
    "transcript",
    "tutor",
    "robinhood",
    "usage",
    "wait",
    "notify",
}
ALIAS_COMMANDS = {
    "archive",
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
    # Click options always carry a non-None type (it defaults to click.STRING).
    if isinstance(option.type, click.Choice):
        choices = " &#x7C; ".join(f"`{c}`" for c in option.type.choices)
        return f"choice ({choices})"
    return option.type.name.lower()


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
    """Infer a description for an argument based on its properties.

    These are best-effort heuristics for arguments that lack explicit metadata.
    The preferred path is to supply ``arguments_description`` in
    ``CommandHelpMetadata`` (handled by ``generate_arguments_section`` above),
    which bypasses these substring guesses entirely.
    """
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


def get_relative_link(from_command: str, to_name: str) -> str:
    """Get the relative markdown link path from one command's doc to another command or topic."""
    from_category = get_command_category(from_command)

    # Check if the target is a command
    to_category = get_command_category(to_name)
    if to_category is not None:
        if from_category == to_category:
            return f"./{to_name}.md"
        else:
            return f"../{to_category}/{to_name}.md"

    # Check if the target is a topic with a docs path
    topic = get_topic(to_name)
    if topic is not None and topic.docs_path is not None:
        from_dir = f"commands/{from_category}" if from_category else "commands"
        return os.path.relpath(topic.docs_path, from_dir)

    # The ref resolves to neither a known command nor a topic with a docs path.
    # Emitting a bare "mngr help <name>" here would produce a broken markdown link
    # (href set to literal text). Fail loudly so `make_cli_docs.py --check` catches
    # the typo'd or stale see_also ref instead of publishing a broken link.
    raise ValueError(
        f"See-Also reference {to_name!r} from command {from_command!r} resolves to "
        f"neither a known command nor a help topic with a docs path; fix the see_also "
        f"metadata for {from_command!r}."
    )


def format_see_also_section(command_name: str, metadata: CommandHelpMetadata) -> str:
    """Format the See Also section from metadata with markdown links.

    A ``ref_name`` of the form ``"list#filtering"`` links to ``list.md#filtering``;
    the bare command name is used for category lookup and link text.
    """
    if not metadata.see_also:
        return ""

    lines = ["", "## See Also", ""]
    for ref_name, description in metadata.see_also:
        bare_name, _, anchor = ref_name.partition("#")
        link = get_relative_link(command_name, bare_name)
        if anchor:
            link = f"{link}#{anchor}"
        # Use "mngr <name>" for commands, "mngr help <name>" for topics
        if get_command_category(bare_name) is not None:
            link_text = f"mngr {bare_name}"
        else:
            link_text = f"mngr help {bare_name}"
        lines.append(f"- [{link_text}]({link}) - {description}")

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
    # An empty group is a legitimate "nothing to document" case. The click.Group
    # type guarantees a .commands attribute, so no hasattr guard is needed.
    if not command.commands:
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


def build_command_doc(command_name: str, base_dir: Path) -> tuple[Path, str] | None:
    """Build the (output path, markdown content) for a single command, or None to skip it."""
    cmd = cli.commands.get(command_name)
    if cmd is None:
        print(f"Warning: Command '{command_name}' not found")
        return None

    # Silently skip hidden commands (internal service commands not intended for users)
    if cmd.hidden:
        return None

    output_dir = get_output_dir(command_name, base_dir)
    if output_dir is None:
        print(f"Skipping: {command_name} (not in PRIMARY_COMMANDS or SECONDARY_COMMANDS)")
        return None

    prog_name = f"mngr {command_name}"
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

    output_file = output_dir / f"{command_name}.md"
    return output_file, content


def build_alias_doc(command_name: str, base_dir: Path) -> tuple[Path, str] | None:
    """Build the (output path, markdown content) for an alias command, or None to skip it.

    Alias commands (like clone, migrate) use UNPROCESSED args and delegate to
    other commands. Their docs are built entirely from CommandHelpMetadata.
    """
    output_dir = get_output_dir(command_name, base_dir)
    if output_dir is None:
        print(f"Skipping: {command_name} (not in ALIAS_COMMANDS)")
        return None

    metadata = get_help_metadata(command_name)
    if metadata is None:
        print(f"Warning: No help metadata for alias command '{command_name}'")
        return None

    content_parts: list[str] = []

    # Title
    content_parts.append(f"# mngr {command_name}")

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

    output_file = output_dir / f"{command_name}.md"
    return output_file, content


GITHUB_BASE_URL = "https://github.com/imbue-ai/mngr/blob/main/"

# Matches markdown link targets: ](path) — but not absolute URLs, anchors, or mailto
_RELATIVE_LINK_RE = re.compile(r"\]\((?!https?://|#|mailto:)([^)]+)\)")


def _local_path_to_github_url(match: re.Match[str]) -> str:
    """Convert a relative markdown link target to a GitHub URL."""
    path = match.group(1)
    return f"]({GITHUB_BASE_URL}{path})"


def build_pypi_readme(repo_root: Path) -> tuple[Path, str]:
    """Build the (output path, content) for libs/mngr/README.md from the top-level README.md.

    Reads the top-level README (which uses local relative paths) and produces
    a version with GitHub absolute URLs for PyPI rendering.
    """
    source = repo_root / "README.md"
    dest = repo_root / "libs" / "mngr" / "README.md"

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

    return dest, content


# -----------------------------------------------------------------------------
# Provider / agent config tables
# -----------------------------------------------------------------------------
# Each plugin README documents its provider/agent config in a markdown table.
# The Description column is the single source of truth in the Pydantic
# ``Field(description=...)`` (also surfaced via ``mngr config``), so we render it
# from the model rather than hand-maintaining a second copy that drifts. Field
# order and the displayed default stay curated here; the table is spliced into
# the README between the BEGIN/END markers.

CONFIG_TABLE_BEGIN = "<!-- BEGIN GENERATED CONFIG TABLE (scripts/make_cli_docs.py) -->"
CONFIG_TABLE_END = "<!-- END GENERATED CONFIG TABLE -->"


class ConfigTableRow(NamedTuple):
    field: str
    default: str


class ConfigTable(NamedTuple):
    readme: str  # path relative to the repo root
    config_cls: type[BaseModel]  # the Pydantic config class whose field descriptions we render
    field_header: str  # label for column 1 (the field / option / setting name)
    description_header: str  # label for column 3
    # Every field declared on ``config_cls`` must have a row -- ``_validate_table_coverage``
    # fails the generator otherwise, so a newly added config field can never silently vanish
    # from the README (the original bug this guard prevents).
    rows: tuple[ConfigTableRow, ...]


CONFIG_TABLES: tuple[ConfigTable, ...] = (
    ConfigTable(
        readme="libs/mngr_aws/README.md",
        config_cls=AwsProviderConfig,
        field_header="Field",
        description_header="Description",
        rows=(
            ConfigTableRow("backend", "`aws`"),
            ConfigTableRow("default_region", "`us-east-1`"),
            ConfigTableRow("default_instance_type", "`t3.small`"),
            ConfigTableRow("default_ami_id", "`None` (pinned Debian 12 amd64 per region)"),
            ConfigTableRow("security_group", '`AutoCreateSecurityGroup(name="mngr-aws")`'),
            ConfigTableRow("subnet_id", "`None`"),
            ConfigTableRow("vpc_id", "`None`"),
            ConfigTableRow("allowed_ssh_cidrs", '`("0.0.0.0/0",)`'),
            ConfigTableRow("associate_public_ip", "`True`"),
            ConfigTableRow("root_volume_size_gb", "`30`"),
            ConfigTableRow("root_volume_type", "`gp3`"),
            ConfigTableRow("iam_instance_profile", "`None`"),
            ConfigTableRow("terminate_on_shutdown", "`false`"),
            ConfigTableRow("auto_shutdown_seconds", "`None`"),
            ConfigTableRow("state_bucket_name", "`None` (auto-derived)"),
            ConfigTableRow("is_offline_host_dir_enabled", "`true`"),
        ),
    ),
    ConfigTable(
        readme="libs/mngr_gcp/README.md",
        config_cls=GcpProviderConfig,
        field_header="Field",
        description_header="Description",
        rows=(
            ConfigTableRow("backend", "`gcp`"),
            ConfigTableRow("project_id", "gcloud/ADC default"),
            ConfigTableRow("default_region", "derived from zone"),
            ConfigTableRow("default_zone", "gcloud `compute/zone`, else `us-west1-a`"),
            ConfigTableRow("default_machine_type", "`e2-small`"),
            ConfigTableRow("default_source_image", "`projects/debian-cloud/global/images/family/debian-12`"),
            ConfigTableRow("boot_disk_size_gb", "`30`"),
            ConfigTableRow("boot_disk_type", "`pd-balanced`"),
            ConfigTableRow("network", "`default`"),
            ConfigTableRow("subnetwork", "`None`"),
            ConfigTableRow("allowed_ssh_cidrs", '`("0.0.0.0/0",)`'),
            ConfigTableRow("firewall_name", "`mngr-gcp-ssh`"),
            ConfigTableRow("firewall_target_tag", "`mngr-ssh`"),
            ConfigTableRow("associate_external_ip", "`True`"),
            ConfigTableRow("service_account_email", "`None`"),
            ConfigTableRow("service_account_scopes", '`("https://www.googleapis.com/auth/cloud-platform",)`'),
            ConfigTableRow("auto_shutdown_seconds", "`None`"),
        ),
    ),
    ConfigTable(
        readme="libs/mngr_ovh/README.md",
        config_cls=OvhProviderConfig,
        field_header="Field",
        description_header="Description",
        rows=(
            ConfigTableRow("backend", "`ovh`"),
            ConfigTableRow("endpoint", "`ovh-us`"),
            ConfigTableRow("application_key", "`None`"),
            ConfigTableRow("application_secret", "`None`"),
            ConfigTableRow("consumer_key", "`None`"),
            ConfigTableRow("client_id", "`None`"),
            ConfigTableRow("client_secret", "`None`"),
            ConfigTableRow("project_id", "`None`"),
            ConfigTableRow("default_region", "`US-EAST-VA`"),
            ConfigTableRow("default_plan", "`vps-2025-model1`"),
            ConfigTableRow("default_image_name", "`Debian 12 - Docker`"),
            ConfigTableRow("bootstrap_ssh_user", "`debian`"),
            ConfigTableRow("pricing_mode", "`default`"),
            ConfigTableRow("duration", "`P1M`"),
            ConfigTableRow("instance_boot_timeout", "`600.0`"),
            ConfigTableRow("ovh_subsidiary", "`US`"),
            ConfigTableRow("enable_recycle_cancelled", "`True`"),
            ConfigTableRow("recycle_safety_margin_hours", "`2`"),
            ConfigTableRow("recycle_max_candidates_considered", "`10`"),
        ),
    ),
    ConfigTable(
        readme="libs/mngr_vultr/README.md",
        config_cls=VultrProviderConfig,
        field_header="Field",
        description_header="Description",
        rows=(
            ConfigTableRow("backend", "`vultr`"),
            ConfigTableRow("api_key", "`None`"),
            ConfigTableRow("default_region", "`ewr`"),
            ConfigTableRow("default_plan", "`vc2-2c-4gb`"),
            ConfigTableRow("default_os_id", "`2136`"),
        ),
    ),
    ConfigTable(
        readme="libs/mngr_vps/README.md",
        config_cls=VpsProviderConfig,
        field_header="Field",
        description_header="Description",
        rows=(
            ConfigTableRow("isolation", "`CONTAINER`"),
            ConfigTableRow("host_dir", "`/mngr`"),
            ConfigTableRow("default_image", "`debian:bookworm-slim`"),
            ConfigTableRow("default_idle_timeout", "800"),
            ConfigTableRow("default_idle_mode", "`IO`"),
            ConfigTableRow("default_activity_sources", "(all sources)"),
            ConfigTableRow("ssh_connect_timeout", "60.0"),
            ConfigTableRow("instance_boot_timeout", "300.0"),
            ConfigTableRow("docker_install_timeout", "300.0"),
            ConfigTableRow("container_ssh_port", "2222"),
            ConfigTableRow("default_region", "`ewr`"),
            ConfigTableRow("default_start_args", "`()`"),
            ConfigTableRow("auto_shutdown_seconds", "`None`"),
            ConfigTableRow("docker_runtime", "`None`"),
            ConfigTableRow("install_gvisor_runtime", "`false`"),
            ConfigTableRow("builder", "`DOCKER`"),
            ConfigTableRow("btrfs_mount_path", "`/mngr-btrfs`"),
            ConfigTableRow("btrfs_loop_file_path", "`/var/lib/mngr-btrfs.img`"),
            ConfigTableRow("outer_disk_reserved_gb", "`20`"),
        ),
    ),
    ConfigTable(
        readme="libs/mngr_opencode/README.md",
        config_cls=OpenCodeAgentConfig,
        field_header="Option",
        description_header="Meaning",
        rows=(
            ConfigTableRow("command", "`opencode`"),
            ConfigTableRow("cli_args", "`()`"),
            ConfigTableRow("config_overrides", "`{}`"),
            ConfigTableRow("sync_global_config", "`true`"),
            ConfigTableRow("symlink_auth", "`true`"),
            ConfigTableRow("auto_allow_permissions", "`false`"),
            ConfigTableRow("check_installation", "`true`"),
            ConfigTableRow("version", "unset"),
            ConfigTableRow("update_policy", "unset"),
            ConfigTableRow("emit_common_transcript", "`true`"),
            ConfigTableRow("preserve_on_destroy", "`true`"),
        ),
    ),
    ConfigTable(
        readme="libs/mngr_pi_coding/README.md",
        config_cls=PiCodingAgentConfig,
        field_header="Setting",
        description_header="Description",
        rows=(
            ConfigTableRow("command", "`pi`"),
            ConfigTableRow("sync_auth", "`true`"),
            ConfigTableRow("sync_home_settings", "`true`"),
            ConfigTableRow("check_installation", "`true`"),
            ConfigTableRow("version", "unset"),
            ConfigTableRow("update_policy", "unset"),
            ConfigTableRow("auto_allow_permissions", "`true`"),
            ConfigTableRow("resume_session", "`true`"),
            ConfigTableRow("emit_common_transcript", "`true`"),
            ConfigTableRow("emit_raw_transcript", "`true`"),
            ConfigTableRow("auto_dismiss_dialogs", "`false`"),
            ConfigTableRow("preserve_on_destroy", "`true`"),
        ),
    ),
)


def _own_field_names(config_cls: type[BaseModel]) -> list[str]:
    """Field names declared directly on ``config_cls`` (not inherited), in declaration order."""
    return [name for name in getattr(config_cls, "__annotations__", {}) if name in config_cls.model_fields]


def _validate_table_coverage(table: ConfigTable) -> None:
    """Fail loudly if a field declared on ``config_cls`` has no row in the table.

    The generator only renders the curated ``rows``, so a field added to the model but
    not to the table would silently disappear from the README. Requiring every own field
    to have a row turns that silent drop into a build failure.
    """
    own = _own_field_names(table.config_cls)
    shown = {row.field for row in table.rows}
    missing = [name for name in own if name not in shown]
    if missing:
        raise ValueError(
            f"{table.readme}: {table.config_cls.__name__} fields {missing} have no row in the table. "
            f"Add a ConfigTableRow for each so the generated README documents them."
        )


def _render_config_table(table: ConfigTable) -> str:
    """Render a markdown table; the Description column comes from the model's field descriptions."""
    _validate_table_coverage(table)
    model_fields = table.config_cls.model_fields
    lines = [
        f"| {table.field_header} | Default | {table.description_header} |",
        "|---|---|---|",
    ]
    for row in table.rows:
        field_info = model_fields.get(row.field)
        if field_info is None:
            raise ValueError(f"{table.config_cls.__name__} has no field {row.field!r} referenced by {table.readme}")
        description = _escape_markdown_table(" ".join((field_info.description or "").split()))
        lines.append(f"| `{row.field}` | {row.default} | {description} |")
    return "\n".join(lines)


def _splice_config_table(readme_text: str, table_md: str, readme: str) -> str:
    """Replace the content between the config-table markers with the rendered table."""
    begin = readme_text.find(CONFIG_TABLE_BEGIN)
    end = readme_text.find(CONFIG_TABLE_END)
    if begin == -1 or end == -1:
        raise ValueError(
            f"{readme} is missing the generated-config-table markers ({CONFIG_TABLE_BEGIN} ... {CONFIG_TABLE_END})"
        )
    if end < begin:
        raise ValueError(f"{readme} has the config-table markers in the wrong order (END before BEGIN)")
    before = readme_text[: begin + len(CONFIG_TABLE_BEGIN)]
    after = readme_text[end:]
    return f"{before}\n{table_md}\n{after}"


def build_config_table_readme(repo_root: Path, table: ConfigTable) -> tuple[Path, str]:
    """Return the (README path, full content) with the generated config table spliced in."""
    path = repo_root / table.readme
    return path, _splice_config_table(path.read_text(), _render_config_table(table), table.readme)


# The exact command a developer runs to regenerate the docs this script owns.
REGEN_COMMAND = "uv run python scripts/make_cli_docs.py"


def collect_generated_files(repo_root: Path) -> dict[Path, str]:
    """Return every generated doc file mapped to its expected content.

    This is the single source of truth shared by ``main``'s writer (the default,
    no-args path, which writes the files) and its checker (the ``--check`` path,
    which only verifies they are up to date), so the writer and checker cannot
    drift apart. ``test_cli_docs_are_up_to_date`` drives the checker by invoking
    the script with ``--check``.
    """
    generated: dict[Path, str] = {}

    # PyPI README from top-level README
    readme_path, readme_content = build_pypi_readme(repo_root)
    generated[readme_path] = readme_content

    # Provider / agent config tables, spliced between markers in each plugin
    # README (Description column generated from the Pydantic field descriptions).
    for table in CONFIG_TABLES:
        path, content = build_config_table_readme(repo_root, table)
        generated[path] = content

    # CLI command docs
    base_dir = repo_root / "libs" / "mngr" / "docs" / "commands"
    for cmd in BUILTIN_COMMANDS + PLUGIN_COMMANDS:
        if cmd.name is not None:
            result = build_command_doc(cmd.name, base_dir)
            if result is not None:
                path, content = result
                generated[path] = content

    # Alias command docs
    for command_name in sorted(ALIAS_COMMANDS):
        result = build_alias_doc(command_name, base_dir)
        if result is not None:
            path, content = result
            generated[path] = content

    return generated


def _find_stale_files(generated: dict[Path, str]) -> list[Path]:
    """Return the generated files whose on-disk content differs from what we'd write."""
    stale: list[Path] = []
    for path, content in generated.items():
        existing_content = path.read_text() if path.exists() else None
        if content != existing_content:
            stale.append(path)
    return stale


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write any files; exit non-zero if any generated doc is out of date.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).parent.parent
    generated = collect_generated_files(repo_root)
    stale = _find_stale_files(generated)

    if args.check:
        if stale:
            print("The following generated docs are out of date:")
            for path in stale:
                print(f"  - {path.relative_to(repo_root)}")
            print(f"\nRun this to regenerate them:\n  {REGEN_COMMAND}")
            sys.exit(1)
        return

    for path in stale:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generated[path])
        print(f"Updated: {path}")


if __name__ == "__main__":
    main()
