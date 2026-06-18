import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any
from typing import Final
from typing import assert_never

import click
from loguru import logger
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement
from pydantic import Field
from tabulate import tabulate

from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.agent_registry import list_available_agent_types
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.config import get_config_path
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.help_formatter import show_help_with_pager
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.cli.plugin_install_wizard import install_wizard
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import ConfigScope
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.config.provider_config_registry import list_registered_provider_backend_names
from imbue.mngr.errors import PluginSpecifierError
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import PluginName
from imbue.mngr.utils.toml_config import set_plugin_enabled
from imbue.mngr.uv_tool import ToolRequirement
from imbue.mngr.uv_tool import build_uv_tool_install_add_requirements
from imbue.mngr.uv_tool import build_uv_tool_install_remove_multiple
from imbue.mngr.uv_tool import has_mngr_entry_points
from imbue.mngr.uv_tool import read_receipt
from imbue.mngr.uv_tool import require_uv_tool_receipt

# Default fields to display
DEFAULT_FIELDS: Final[tuple[str, ...]] = ("name", "version", "description", "enabled")


class PluginCliOptions(CommonCliOptions):
    """Options passed from the CLI to the plugin command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the plugin() function itself.
    """

    is_active: bool = False
    fields: str | None = None
    kind: str | None = None
    name: str | None = None
    names: tuple[str, ...] = ()
    scope: str | None = None
    path: tuple[str, ...] = ()
    git: tuple[str, ...] = ()


class PluginInfo(FrozenModel):
    """Information about a discovered plugin."""

    name: str = Field(description="Plugin name")
    version: str | None = Field(default=None, description="Plugin version from distribution metadata")
    description: str | None = Field(default=None, description="Plugin description from distribution metadata")
    is_enabled: bool = Field(description="Whether the plugin is currently enabled")


@pure
def _is_plugin_enabled(name: str, config: MngrConfig) -> bool:
    """Check whether a plugin is enabled based on config.

    A plugin is disabled if:
    1. Its name is in the disabled_plugins set, OR
    2. It appears in the plugins dict with enabled=False
    """
    if name in config.disabled_plugins:
        return False
    plugin_key = PluginName(name)
    if plugin_key in config.plugins and not config.plugins[plugin_key].enabled:
        return False
    return True


def _gather_plugin_info(mngr_ctx: MngrContext) -> list[PluginInfo]:
    """Discover plugins from the plugin manager and return sorted info.

    Uses pm.list_name_plugin() for all registered plugins and
    pm.list_plugin_distinfo() for distribution metadata (version, description).

    Also includes disabled plugins that were blocked from registration
    (via pm.set_blocked) so they still appear in `mngr plugin list`.
    """
    pm = mngr_ctx.pm

    # Build a map of plugin object id -> dist metadata from externally installed plugins
    dist_info_by_plugin: dict[int, Any] = {}
    for plugin_obj, dist in pm.list_plugin_distinfo():
        dist_info_by_plugin[id(plugin_obj)] = dist

    # Gather info for all registered plugins
    plugin_info_by_name: dict[str, PluginInfo] = {}
    for name, plugin_obj in pm.list_name_plugin():
        if name is None:
            continue
        # Skip internal pluggy marker plugins
        if name.startswith("_"):
            continue

        version: str | None = None
        description: str | None = None

        # Check for distribution metadata
        dist = dist_info_by_plugin.get(id(plugin_obj))
        if dist is not None:
            metadata = dist.metadata
            version = metadata.get("version")
            description = metadata.get("summary")

        # pm is the ground truth for whether a plugin is active: a blocked plugin
        # is never registered (pluggy still lists its name here with a None plugin
        # object), so report it as disabled regardless of config. This catches
        # blocks that config.disabled_plugins does not record -- e.g. command-default
        # or create-template --disable-plugin, applied after load_config in
        # setup_command_context -- without which such plugins would be mislabeled
        # enabled.
        is_enabled = _is_plugin_enabled(name, mngr_ctx.config) and not pm.is_blocked(name)

        plugin_info_by_name[name] = PluginInfo(
            name=name,
            version=version,
            description=description,
            is_enabled=is_enabled,
        )

    # Include disabled plugins that were blocked and never registered.
    # These won't appear in pm.list_name_plugin() but should still be
    # visible in the plugin list so users can see and re-enable them.
    # Version/description are unavailable because pluggy doesn't expose
    # metadata for blocked plugins.
    for disabled_name in mngr_ctx.config.disabled_plugins:
        if disabled_name not in plugin_info_by_name:
            plugin_info_by_name[disabled_name] = PluginInfo(
                name=disabled_name,
                version=None,
                description=None,
                is_enabled=False,
            )

    return sorted(plugin_info_by_name.values(), key=lambda p: p.name)


@pure
def _get_field_value(plugin: PluginInfo, field: str) -> str:
    """Get a display value for a plugin field."""
    match field:
        case "name":
            return plugin.name
        case "version":
            return plugin.version or "-"
        case "description":
            return plugin.description or "-"
        case "enabled":
            return str(plugin.is_enabled).lower()
        case _:
            return "-"


def _emit_plugin_list(
    plugins: list[PluginInfo],
    output_opts: OutputOptions,
    fields: tuple[str, ...],
) -> None:
    """Emit the plugin list in the appropriate output format."""
    if output_opts.format_template is not None:
        items = [{f: _get_field_value(p, f) for f in DEFAULT_FIELDS} for p in plugins]
        emit_format_template_lines(output_opts.format_template, items)
        return
    match output_opts.output_format:
        case OutputFormat.HUMAN:
            _emit_plugin_list_human(plugins, fields)
        case OutputFormat.JSON:
            _emit_plugin_list_json(plugins, fields)
        case OutputFormat.JSONL:
            _emit_plugin_list_jsonl(plugins, fields)
        case _ as unreachable:
            assert_never(unreachable)


def _emit_plugin_list_human(plugins: list[PluginInfo], fields: tuple[str, ...]) -> None:
    """Emit plugin list in human-readable table format."""
    if not plugins:
        write_human_line("No plugins found.")
        return

    headers = [f.upper() for f in fields]
    rows: list[list[str]] = []
    for p in plugins:
        rows.append([_get_field_value(p, f) for f in fields])

    table = tabulate(rows, headers=headers, tablefmt="plain")
    write_human_line("\n" + table)


def _emit_plugin_list_json(plugins: list[PluginInfo], fields: tuple[str, ...]) -> None:
    """Emit plugin list in JSON format."""
    plugin_dicts = [{f: _get_field_value(p, f) for f in fields} for p in plugins]
    write_json_line({"plugins": plugin_dicts})


def _emit_plugin_list_jsonl(plugins: list[PluginInfo], fields: tuple[str, ...]) -> None:
    """Emit plugin list in JSONL format (one line per plugin)."""
    for p in plugins:
        write_json_line({f: _get_field_value(p, f) for f in fields})


@pure
def _parse_fields(fields_str: str | None) -> tuple[str, ...]:
    """Parse a comma-separated fields string into a tuple of field names."""
    if fields_str is None:
        return DEFAULT_FIELDS
    return tuple(f.strip() for f in fields_str.split(",") if f.strip())


@pure
def _parse_pypi_package_name(specifier: str) -> str | None:
    """Extract the canonical package name from a PyPI requirement string.

    Parses specifiers like 'imbue-mngr-opencode>=1.0' and returns just the name
    ('imbue-mngr-opencode'). Returns None if the specifier is not a valid PyPI
    requirement.
    """
    try:
        requirement = Requirement(specifier)
    except InvalidRequirement:
        return None
    return requirement.name


def _get_installed_package_names(concurrency_group: Any) -> set[str]:
    """Get the set of currently installed package names via ``uv pip list``."""
    result = concurrency_group.run_process_to_completion(
        ("uv", "pip", "list", "--python", sys.executable, "--format", "json")
    )
    packages = json.loads(result.stdout)
    return {pkg["name"] for pkg in packages}


def _read_package_name_from_pyproject(local_path: str) -> str:
    """Read the package name from a local path's pyproject.toml.

    Raises PluginSpecifierError if the file is missing or has no project.name.
    """
    resolved = Path(local_path).expanduser().resolve()
    pyproject_path = resolved / "pyproject.toml"
    if not pyproject_path.exists():
        raise PluginSpecifierError(f"No pyproject.toml found at '{resolved}' -- cannot determine package name")
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    name = data.get("project", {}).get("name")
    if not name:
        raise PluginSpecifierError(f"pyproject.toml at '{resolved}' does not have a project.name field")
    return name


def _emit_plugin_add_result(
    specifier: str,
    package_name: str,
    has_entry_points: bool,
    output_opts: OutputOptions,
) -> None:
    """Emit the result of a plugin add operation."""
    match output_opts.output_format:
        case OutputFormat.HUMAN:
            write_human_line("Installed plugin package '{}'", package_name)
            if not has_entry_points:
                logger.warning(
                    "Package installed but no mngr entry points found -- this package may not be a mngr plugin"
                )
        case OutputFormat.JSON:
            write_json_line(
                {
                    "specifier": specifier,
                    "package": package_name,
                    "has_entry_points": has_entry_points,
                }
            )
        case OutputFormat.JSONL:
            write_json_line(
                {
                    "event": "plugin_added",
                    "specifier": specifier,
                    "package": package_name,
                    "has_entry_points": has_entry_points,
                }
            )
        case _ as unreachable:
            assert_never(unreachable)


def _emit_plugin_remove_result(
    package_name: str,
    output_opts: OutputOptions,
) -> None:
    """Emit the result of a plugin remove operation."""
    match output_opts.output_format:
        case OutputFormat.HUMAN:
            write_human_line("Removed plugin package '{}'", package_name)
        case OutputFormat.JSON:
            write_json_line(
                {
                    "package": package_name,
                }
            )
        case OutputFormat.JSONL:
            write_json_line(
                {
                    "event": "plugin_removed",
                    "package": package_name,
                }
            )
        case _ as unreachable:
            assert_never(unreachable)


@click.group(name="plugin", invoke_without_command=True)
@add_common_options
@click.pass_context
def plugin(ctx: click.Context, **kwargs: Any) -> None:
    if ctx.invoked_subcommand is None:
        show_help_with_pager(ctx, ctx.command, None)


@plugin.command(name="list")
@click.option(
    "--active",
    "is_active",
    is_flag=True,
    default=False,
    help="Show only currently enabled plugins",
)
@click.option(
    "--fields",
    type=str,
    default=None,
    help="Comma-separated list of fields to display (name, version, description, enabled)",
)
@click.option(
    "--kind",
    type=click.Choice(["agent-type", "provider"], case_sensitive=False),
    default=None,
    help="Filter to plugins of a specific kind: agent-type or provider",
)
@add_common_options
@click.pass_context
def plugin_list(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _plugin_list_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _plugin_list_impl(ctx: click.Context, **kwargs: Any) -> None:
    """Implementation of plugin list command."""
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="plugin",
        command_class=PluginCliOptions,
        is_format_template_supported=True,
    )

    all_plugins = _gather_plugin_info(mngr_ctx)

    # Filter to active plugins if requested
    filtered_plugins = [p for p in all_plugins if p.is_enabled] if opts.is_active else all_plugins

    # Filter to a specific plugin kind if requested. ``opts.kind`` is
    # constrained by click.Choice, so the unreachable arm catches drift.
    match opts.kind:
        case "agent-type":
            filtered_plugins = _project_to_agent_type_entries(filtered_plugins, mngr_ctx.config)
        case "provider":
            filtered_plugins = _project_to_provider_entries(filtered_plugins)
        case None:
            # No --kind requested -- leave filtered_plugins as-is.
            pass
        case _:
            raise AbortError(f"Unknown --kind value: {opts.kind!r}")

    fields = _parse_fields(opts.fields)
    _emit_plugin_list(filtered_plugins, output_opts, fields)


def _project_to_agent_type_entries(plugins: list[PluginInfo], config: MngrConfig) -> list[PluginInfo]:
    """Return a PluginInfo list keyed by every available agent type name.

    Plugin entry-point names do not always match the agent-type name they
    register (e.g. the pi_coding plugin -- entry-point name ``pi_coding``
    -- registers an agent type named ``pi-coding``), and user config can
    also define agent types under ``[agent_types.X]`` that are not backed
    by a plugin entry point. We iterate ``list_available_agent_types`` --
    the same source the tab-completion cache uses -- and reuse the
    existing ``PluginInfo`` (with version/description) when an entry-point
    name happens to match, otherwise synthesize a minimal entry.

    ``--active`` filtering is honored upstream by ``pm.set_blocked``:
    plugins disabled in config are blocked before
    ``load_setuptools_entrypoints``, so their ``register_agent_type``
    hookimpl never runs and their agent types are absent from
    ``list_available_agent_types(config)``. The ``plugins`` argument is
    therefore consulted only for metadata, not for filtering -- otherwise
    agent types like ``pi-coding`` would be silently dropped because no
    entry-point of that name exists.
    """
    existing_by_name = {p.name: p for p in plugins}
    return [
        existing_by_name.get(name, PluginInfo(name=name, is_enabled=True))
        for name in list_available_agent_types(config)
    ]


def _project_to_provider_entries(plugins: list[PluginInfo]) -> list[PluginInfo]:
    """Return a PluginInfo list keyed by every registered provider backend name.

    Mirror of ``_project_to_agent_type_entries`` for provider backends.
    Reuses the existing ``PluginInfo`` (with version/description) when an
    entry-point name matches a backend name (the common case -- e.g.
    ``docker``, ``modal``, ``vultr``); otherwise synthesizes a minimal
    entry. Provider backends are plugin-registered only -- there is no
    user-config equivalent to ``[agent_types.X]`` for backends -- so the
    list comes solely from ``list_registered_provider_backend_names``.
    """
    existing_by_name = {p.name: p for p in plugins}
    return [
        existing_by_name.get(name, PluginInfo(name=name, is_enabled=True))
        for name in list_registered_provider_backend_names()
    ]


@plugin.command(name="add")
@click.argument("names", nargs=-1)
@click.option("--path", multiple=True, help="Install from a local path (editable mode) [repeatable]")
@click.option("--git", multiple=True, help="Install from a git URL [repeatable]")
@add_common_options
@click.pass_context
def plugin_add(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _plugin_add_impl(ctx)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


@plugin.command(name="remove")
@click.argument("names", nargs=-1)
@click.option(
    "--path", multiple=True, help="Remove by local path (reads package name from pyproject.toml) [repeatable]"
)
@add_common_options
@click.pass_context
def plugin_remove(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _plugin_remove_impl(ctx)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


class _PypiSource(FrozenModel):
    """Plugin source: a PyPI package name (possibly with version constraint)."""

    name: str = Field(description="PyPI package specifier (e.g. 'imbue-mngr-opencode>=1.0')")


class _PathSource(FrozenModel):
    """Plugin source: a local filesystem path."""

    path: str = Field(description="Local filesystem path to the plugin package")


class _GitSource(FrozenModel):
    """Plugin source: a git URL."""

    url: str = Field(description="Git repository URL for the plugin package")


_AddSource = _PypiSource | _PathSource | _GitSource
_RemoveSource = _PypiSource | _PathSource


@pure
def _parse_add_sources(opts: PluginCliOptions) -> list[_AddSource]:
    """Parse and validate the plugin source(s) for an add command.

    All source types (positional names, --path, --git) can be freely mixed
    and each is repeatable. At least one source must be provided.
    """
    sources: list[_AddSource] = []

    for name in opts.names:
        if _parse_pypi_package_name(name) is None:
            raise AbortError(f"Invalid package name '{name}'")
        sources.append(_PypiSource(name=name))

    for path in opts.path:
        sources.append(_PathSource(path=path))

    for url in opts.git:
        sources.append(_GitSource(url=url))

    if not sources:
        raise AbortError("Provide at least one of NAME, --path, or --git")

    return sources


@pure
def _parse_remove_sources(opts: PluginCliOptions) -> list[_RemoveSource]:
    """Parse and validate the plugin source(s) for a remove command.

    Both source types (positional names, --path) can be freely mixed
    and each is repeatable. At least one source must be provided.
    """
    sources: list[_RemoveSource] = []

    for name in opts.names:
        if _parse_pypi_package_name(name) is None:
            raise AbortError(f"Invalid package name '{name}'")
        sources.append(_PypiSource(name=name))

    for path in opts.path:
        sources.append(_PathSource(path=path))

    if not sources:
        raise AbortError("Provide at least one of NAME or --path")

    return sources


def _plugin_add_impl(ctx: click.Context) -> None:
    """Implementation of plugin add command."""
    # ``mngr plugin add`` typically runs against a config that references the
    # plugin about to be installed (e.g. user pre-declares ``[providers.modal]``
    # then runs ``mngr plugin add imbue-mngr-modal``). ``strict=False`` keeps the
    # command from erroring on unknown-field/unknown-backend references, and
    # ``silent_unknown_fields=True`` suppresses the warnings that would
    # otherwise be emitted for those references -- they're noise that resolves
    # itself the moment the install completes.
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="plugin",
        command_class=PluginCliOptions,
        strict=False,
        silent_unknown_fields=True,
    )

    # Validate arguments before checking uv tool receipt so users get clear
    # argument-validation errors rather than a "not installed via uv tool" error.
    sources = _parse_add_sources(opts)

    receipt_path = require_uv_tool_receipt()
    receipt = read_receipt(receipt_path)

    # Build new requirements and track metadata for each source.
    # Each entry in source_info is (specifier, resolved_package_name, is_git).
    # For git sources, resolved_package_name is set to the URL initially and
    # updated after install by diffing the installed packages.
    new_requirements: list[ToolRequirement] = []
    source_info: list[tuple[str, str, bool]] = []
    has_git_source = False

    for source in sources:
        match source:
            case _PathSource(path=path):
                resolved_path = str(Path(path).expanduser().resolve())
                try:
                    package_name = _read_package_name_from_pyproject(path)
                except PluginSpecifierError:
                    logger.debug("Could not read package name from pyproject.toml at '{}', using raw path", path)
                    package_name = path
                new_requirements.append(ToolRequirement(name=package_name, editable=resolved_path))
                source_info.append((path, package_name, False))
            case _GitSource(url=url):
                git_url = url if url.startswith("git+") else f"git+{url}"
                new_requirements.append(ToolRequirement(name=git_url))
                source_info.append((url, url, True))
                has_git_source = True
            case _PypiSource(name=name):
                new_requirements.append(ToolRequirement(name=name))
                source_info.append((name, _parse_pypi_package_name(name) or name, False))
            case _ as unreachable:
                assert_never(unreachable)

    # For git installs, snapshot installed packages before install so we can diff afterward
    packages_before: set[str] | None = None
    if has_git_source:
        packages_before = _get_installed_package_names(mngr_ctx.concurrency_group)

    # Build a single uv tool install command with all new requirements
    command = build_uv_tool_install_add_requirements(receipt, new_requirements)

    all_specifiers = ", ".join(spec for spec, _, _ in source_info)
    with log_span("Installing plugin packages: {}", all_specifiers):
        try:
            mngr_ctx.concurrency_group.run_process_to_completion(command)
        except ProcessError as e:
            raise AbortError(
                f"Failed to install plugin packages: {e.stderr.strip() or e.stdout.strip()}",
                original_exception=e,
            ) from e

    # For git installs, resolve canonical package names by diffing installed packages
    if has_git_source:
        assert packages_before is not None
        packages_after = _get_installed_package_names(mngr_ctx.concurrency_group)
        new_packages = packages_after - packages_before
        # Best-effort: assign new package names to git sources in order.
        # When multiple git sources are installed, we cannot reliably
        # map each URL to its package name, so we assign in iteration order.
        new_names_iter = iter(new_packages)
        source_info = [
            (spec, next(new_names_iter, url), is_git) if is_git else (spec, url, is_git)
            for spec, url, is_git in source_info
        ]

    # Report results for each source
    for specifier, resolved_package_name, _ in source_info:
        has_entry_points = has_mngr_entry_points(resolved_package_name)
        _emit_plugin_add_result(specifier, resolved_package_name, has_entry_points, output_opts)


def _plugin_remove_impl(ctx: click.Context) -> None:
    """Implementation of plugin remove command."""
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="plugin",
        command_class=PluginCliOptions,
    )

    # Validate arguments before checking uv tool receipt so users get clear
    # argument-validation errors rather than a "not installed via uv tool" error.
    sources = _parse_remove_sources(opts)

    receipt_path = require_uv_tool_receipt()
    receipt = read_receipt(receipt_path)

    # Resolve package names for all sources
    package_names: list[str] = []
    for source in sources:
        match source:
            case _PathSource(path=path):
                try:
                    package_names.append(_read_package_name_from_pyproject(path))
                except PluginSpecifierError as e:
                    raise AbortError(str(e)) from e
            case _PypiSource(name=name):
                package_names.append(_parse_pypi_package_name(name) or name)
            case _ as unreachable:
                assert_never(unreachable)

    # Verify all packages are actually dependencies before trying to remove
    extra_names = {r.name for r in receipt.extras}
    for package_name in package_names:
        if package_name not in extra_names:
            raise AbortError(f"Package '{package_name}' is not installed as a plugin")

    # Build a single command that removes all requested packages
    command = build_uv_tool_install_remove_multiple(receipt, set(package_names))

    all_names = ", ".join(package_names)
    with log_span("Removing plugin packages: {}", all_names):
        try:
            mngr_ctx.concurrency_group.run_process_to_completion(command)
        except ProcessError as e:
            raise AbortError(
                f"Failed to remove plugin packages: {e.stderr.strip() or e.stdout.strip()}",
                original_exception=e,
            ) from e

    for package_name in package_names:
        _emit_plugin_remove_result(package_name, output_opts)


# FOLLOWUP: in addition to the above, I also want a sub-command for "mngr plugin search" so that you can easily search across all plugins (once there are a bunch of them)
# FOLLOWUP: in addition to the above, I also want a sub-command for "mngr plugin generate" so that you can easily create your own plugin for basically any functionality you want (and then publish it for others to use or take inspiration from!)


@plugin.command(name="enable")
@click.argument("name")
@click.option(
    "--scope",
    type=click.Choice(["user", "project", "local"], case_sensitive=False),
    default="project",
    show_default=True,
    help="Config scope: user (~/.mngr/profiles/<profile_id>/), project (.mngr/), or local (.mngr/settings.local.toml)",
)
@add_common_options
@click.pass_context
def plugin_enable(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _plugin_enable_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


@plugin.command(name="disable")
@click.argument("name")
@click.option(
    "--scope",
    type=click.Choice(["user", "project", "local"], case_sensitive=False),
    default="project",
    show_default=True,
    help="Config scope: user (~/.mngr/profiles/<profile_id>/), project (.mngr/), or local (.mngr/settings.local.toml)",
)
@add_common_options
@click.pass_context
def plugin_disable(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _plugin_disable_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _plugin_enable_impl(ctx: click.Context, **kwargs: Any) -> None:
    """Implementation of plugin enable command."""
    _plugin_set_enabled_impl(ctx, is_enabled=True)


def _plugin_disable_impl(ctx: click.Context, **kwargs: Any) -> None:
    """Implementation of plugin disable command."""
    _plugin_set_enabled_impl(ctx, is_enabled=False)


def _plugin_set_enabled_impl(ctx: click.Context, *, is_enabled: bool) -> None:
    """Shared implementation for plugin enable/disable commands."""
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="plugin",
        command_class=PluginCliOptions,
    )

    name = opts.name
    if name is None:
        raise AbortError("Plugin name is required")

    _validate_plugin_name_is_known(name, mngr_ctx)

    root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")
    scope = ConfigScope((opts.scope or "project").upper())
    config_path = get_config_path(scope, root_name, mngr_ctx.profile_dir, mngr_ctx.concurrency_group)

    set_plugin_enabled(name, is_enabled=is_enabled, config_path=config_path)

    _emit_plugin_toggle_result(name, is_enabled, scope, config_path, output_opts)


def _validate_plugin_name_is_known(name: str, mngr_ctx: MngrContext) -> None:
    """Warn if the plugin name is not registered with the plugin manager.

    This is a soft validation: the user may be pre-configuring a plugin
    before installing it.
    """
    known_names = {n for n, _ in mngr_ctx.pm.list_name_plugin() if n is not None}
    if name not in known_names:
        logger.warning("Plugin '{}' is not currently registered; setting will apply when it is installed", name)


def _emit_plugin_toggle_result(
    name: str,
    is_enabled: bool,
    scope: ConfigScope,
    config_path: Path,
    output_opts: OutputOptions,
) -> None:
    """Emit the result of a plugin enable/disable operation."""
    match output_opts.output_format:
        case OutputFormat.HUMAN:
            action = "Enabled" if is_enabled else "Disabled"
            write_human_line("{} plugin '{}' in {} ({})", action, name, scope.value.lower(), config_path)
        case OutputFormat.JSON:
            write_json_line(
                {
                    "plugin": name,
                    "enabled": is_enabled,
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.JSONL:
            write_json_line(
                {
                    "event": "plugin_toggled",
                    "plugin": name,
                    "enabled": is_enabled,
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case _ as unreachable:
            assert_never(unreachable)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="plugin",
    one_line_description="Manage available and active plugins",
    synopsis="mngr [plugin|plug] <subcommand> [OPTIONS]",
    description="""Install, remove, view, enable, and disable plugins registered with mngr.
Plugins provide agent types, provider backends, CLI commands, and lifecycle hooks.""",
    aliases=("plug",),
    examples=(
        ("List all plugins", "mngr plugin list"),
        ("List only active plugins", "mngr plugin list --active"),
        ("List plugins as JSON", "mngr plugin list --format json"),
        ("Show specific fields", "mngr plugin list --fields name,enabled"),
        ("Install a plugin from PyPI", "mngr plugin add imbue-mngr-pair"),
        ("Install a local plugin", "mngr plugin add --path ./my-plugin"),
        ("Install multiple plugins at once", "mngr plugin add pkg-a --path ./local-b --git https://example.com/c.git"),
        ("Remove a plugin", "mngr plugin remove imbue-mngr-pair"),
        ("Enable a plugin", "mngr plugin enable modal"),
        ("Disable a plugin", "mngr plugin disable modal --scope user"),
    ),
    see_also=(("config", "Manage mngr configuration"),),
).register()

add_pager_help_option(plugin)

# -- Subcommand help metadata --

CommandHelpMetadata(
    key="plugin.list",
    one_line_description="List discovered plugins",
    synopsis="mngr plugin list [OPTIONS]",
    description="""Shows all plugins registered with mngr, including built-in plugins
and any externally installed plugins.

Supports custom format templates via --format. Available fields:
name, version, description, enabled.""",
    examples=(
        ("List all plugins", "mngr plugin list"),
        ("List only active plugins", "mngr plugin list --active"),
        ("List installed agent-type plugins", "mngr plugin list --kind agent-type --active"),
        ("List installed provider plugins", "mngr plugin list --kind provider --active"),
        ("Output as JSON", "mngr plugin list --format json"),
        ("Show specific fields", "mngr plugin list --fields name,enabled"),
        ("Custom format template", "mngr plugin list --format '{name}\\t{enabled}'"),
    ),
    see_also=(
        ("plugin add", "Install a plugin package"),
        ("plugin enable", "Enable a plugin"),
    ),
).register()
add_pager_help_option(plugin_list)

CommandHelpMetadata(
    key="plugin.add",
    one_line_description="Install a plugin package",
    synopsis="mngr plugin add [NAME...] [OPTIONS]",
    description="""All source types are repeatable and can be freely mixed in one command.
NAME is a PyPI package specifier (e.g., 'imbue-mngr-pair' or 'imbue-mngr-pair>=1.0').
--path installs from a local directory in editable mode.
--git installs from a git URL.
All plugins are installed in a single operation for speed.""",
    examples=(
        ("Install from PyPI", "mngr plugin add imbue-mngr-pair"),
        ("Install with version constraint", "mngr plugin add imbue-mngr-pair>=1.0"),
        ("Install from a local path", "mngr plugin add --path ./my-plugin"),
        ("Install multiple local plugins", "mngr plugin add --path ./plugin-a --path ./plugin-b"),
        ("Install from a git URL", "mngr plugin add --git https://github.com/user/mngr-plugin.git"),
        ("Mix all source types", "mngr plugin add pkg-a --path ./local-b --git https://example.com/c.git"),
    ),
    see_also=(
        ("plugin remove", "Uninstall a plugin package"),
        ("plugin list", "List discovered plugins"),
    ),
).register()
add_pager_help_option(plugin_add)

CommandHelpMetadata(
    key="plugin.remove",
    one_line_description="Uninstall a plugin package",
    synopsis="mngr plugin remove [NAME...] [OPTIONS]",
    description="""Both source types are repeatable and can be freely mixed in one command.
For local paths, the package name is read from pyproject.toml.
All plugins are removed in a single operation.""",
    examples=(
        ("Remove by name", "mngr plugin remove imbue-mngr-pair"),
        ("Remove multiple by name", "mngr plugin remove imbue-mngr-pair imbue-mngr-opencode"),
        ("Remove by local path", "mngr plugin remove --path ./my-plugin"),
        ("Mix names and paths", "mngr plugin remove imbue-mngr-pair --path ./my-plugin"),
    ),
    see_also=(
        ("plugin add", "Install a plugin package"),
        ("plugin list", "List discovered plugins"),
    ),
).register()
add_pager_help_option(plugin_remove)

CommandHelpMetadata(
    key="plugin.enable",
    one_line_description="Enable a plugin",
    synopsis="mngr plugin enable NAME [OPTIONS]",
    description="""Sets plugins.<name>.enabled = true in the configuration file at the
specified scope.""",
    examples=(
        ("Enable at project scope (default)", "mngr plugin enable modal"),
        ("Enable at user scope", "mngr plugin enable modal --scope user"),
        ("Output as JSON", "mngr plugin enable modal --format json"),
    ),
    see_also=(
        ("plugin disable", "Disable a plugin"),
        ("plugin list", "List discovered plugins"),
    ),
).register()
add_pager_help_option(plugin_enable)

CommandHelpMetadata(
    key="plugin.disable",
    one_line_description="Disable a plugin",
    synopsis="mngr plugin disable NAME [OPTIONS]",
    description="""Sets plugins.<name>.enabled = false in the configuration file at the
specified scope.""",
    examples=(
        ("Disable at project scope (default)", "mngr plugin disable modal"),
        ("Disable at user scope", "mngr plugin disable modal --scope user"),
        ("Output as JSON", "mngr plugin disable modal --format json"),
    ),
    see_also=(
        ("plugin enable", "Enable a plugin"),
        ("plugin list", "List discovered plugins"),
    ),
).register()
add_pager_help_option(plugin_disable)

# -- install-wizard subcommand (defined in a separate module to keep this file manageable) --

plugin.add_command(install_wizard)
