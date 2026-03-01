import json
import os
import subprocess
import tomllib
from collections.abc import MutableMapping
from enum import auto
from pathlib import Path
from typing import Any
from typing import assert_never
from typing import cast

import click
import tomlkit
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.mng.cli.common_opts import CommonCliOptions
from imbue.mng.cli.common_opts import add_common_options
from imbue.mng.cli.common_opts import setup_command_context
from imbue.mng.cli.help_formatter import CommandHelpMetadata
from imbue.mng.cli.help_formatter import add_pager_help_option
from imbue.mng.cli.help_formatter import show_help_with_pager
from imbue.mng.cli.output_helpers import AbortError
from imbue.mng.cli.output_helpers import emit_final_json
from imbue.mng.cli.output_helpers import emit_format_template_lines
from imbue.mng.cli.output_helpers import write_human_line
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.config.loader import parse_config
from imbue.mng.config.pre_readers import get_local_config_name
from imbue.mng.config.pre_readers import get_project_config_name
from imbue.mng.config.pre_readers import get_user_config_path
from imbue.mng.errors import ConfigKeyNotFoundError
from imbue.mng.errors import ConfigNotFoundError
from imbue.mng.errors import ConfigParseError
from imbue.mng.errors import ConfigStructureError
from imbue.mng.primitives import OutputFormat
from imbue.mng.utils.file_utils import atomic_write
from imbue.mng.utils.git_utils import find_git_worktree_root
from imbue.mng.utils.interactive_subprocess import run_interactive_subprocess


class ConfigScope(UpperCaseStrEnum):
    """Scope for configuration file operations."""

    USER = auto()
    PROJECT = auto()
    LOCAL = auto()


class ConfigCliOptions(CommonCliOptions):
    """Options passed from the CLI to the config command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the config() function itself.
    """

    scope: str | None
    # Arguments used by subcommands (get, set, unset)
    key: str | None = None
    value: str | None = None


def get_config_path(scope: ConfigScope, root_name: str, profile_dir: Path, cg: ConcurrencyGroup) -> Path:
    """Get the config file path for the given scope. The profile_dir is required for USER scope."""
    match scope:
        case ConfigScope.USER:
            if profile_dir is None:
                raise ConfigNotFoundError("profile_dir is required for USER scope")
            return get_user_config_path(profile_dir)
        case ConfigScope.PROJECT:
            git_root = find_git_worktree_root(None, cg) if cg is not None else None
            if git_root is None:
                raise ConfigNotFoundError("No git repository found for project config")
            return git_root / get_project_config_name(root_name)
        case ConfigScope.LOCAL:
            git_root = find_git_worktree_root(None, cg) if cg is not None else None
            if git_root is None:
                raise ConfigNotFoundError("No git repository found for local config")
            return git_root / get_local_config_name(root_name)
        case _ as unreachable:
            assert_never(unreachable)


def _load_config_file(path: Path) -> dict[str, Any]:
    """Load a TOML config file."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_config_file_tomlkit(path: Path) -> tomlkit.TOMLDocument:
    """Load a TOML config file using tomlkit for preservation of formatting."""
    if not path.exists():
        return tomlkit.document()
    with open(path) as f:
        return tomlkit.load(f)


def save_config_file(path: Path, doc: tomlkit.TOMLDocument) -> None:
    """Save a TOML config file atomically."""
    atomic_write(path, tomlkit.dumps(doc))


def _get_nested_value(data: dict[str, Any], key_path: str) -> Any:
    """Get a value from nested dict using dot-separated key path."""
    keys = key_path.split(".")
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise ConfigKeyNotFoundError(key_path)
        current = current[key]
    return current


def set_nested_value(doc: tomlkit.TOMLDocument, key_path: str, value: Any) -> None:
    """Set a value in nested tomlkit document using dot-separated key path.

    Works with tomlkit's TOMLDocument and Table types, which both behave like
    MutableMapping at runtime even though their type stubs don't perfectly reflect this.
    """
    keys = key_path.split(".")
    # tomlkit's TOMLDocument and Table are dict subclasses at runtime
    current: MutableMapping[str, Any] = doc
    for key in keys[:-1]:
        if key not in current:
            current[key] = tomlkit.table()
        next_val = current[key]
        if not isinstance(next_val, dict):
            raise ConfigStructureError(f"Cannot set nested key: {key} is not a table")
        # Cast is needed because tomlkit stubs don't reflect that Table is a dict
        current = cast(MutableMapping[str, Any], next_val)
    current[keys[-1]] = value


def _unset_nested_value(doc: tomlkit.TOMLDocument, key_path: str) -> bool:
    """Remove a value from nested tomlkit document using dot-separated key path.

    Returns True if the value was found and removed, False otherwise.

    Works with tomlkit's TOMLDocument and Table types, which both behave like
    MutableMapping at runtime even though their type stubs don't perfectly reflect this.
    """
    keys = key_path.split(".")
    # tomlkit's TOMLDocument and Table are dict subclasses at runtime
    current: MutableMapping[str, Any] = doc
    for key in keys[:-1]:
        if key not in current:
            return False
        next_val = current[key]
        if not isinstance(next_val, dict):
            return False
        # Cast is needed because tomlkit stubs don't reflect that Table is a dict
        current = cast(MutableMapping[str, Any], next_val)
    if keys[-1] in current:
        del current[keys[-1]]
        return True
    return False


def _parse_value(value_str: str) -> Any:
    """Parse a string value into the appropriate type.

    Attempts to parse as JSON first (for booleans, numbers, arrays, objects),
    then falls back to treating it as a string.
    """
    # Try parsing as JSON for proper type handling
    try:
        return json.loads(value_str)
    except json.JSONDecodeError:
        # Not valid JSON, treat as string
        return value_str


def _format_value_for_display(value: Any) -> str:
    """Format a value for human-readable display."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _flatten_config(config: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    """Flatten a nested config dict into a list of (key_path, value) tuples."""
    result: list[tuple[str, Any]] = []
    for key, value in config.items():
        full_key = f"{prefix}{key}" if prefix else key
        if isinstance(value, dict):
            result.extend(_flatten_config(value, f"{full_key}."))
        else:
            result.append((full_key, value))
    return result


@click.group(name="config", invoke_without_command=True)
@click.option(
    "--scope",
    type=click.Choice(["user", "project", "local"], case_sensitive=False),
    help="Config scope: user (~/.mng/profiles/<profile_id>/), project (.mng/), or local (.mng/settings.local.toml)",
)
@add_common_options
@click.pass_context
def config(ctx: click.Context, **kwargs: Any) -> None:
    if ctx.invoked_subcommand is None:
        mng_ctx, _, _ = setup_command_context(
            ctx=ctx,
            command_name="config",
            command_class=ConfigCliOptions,
        )
        show_help_with_pager(ctx, ctx.command, mng_ctx.config)


@config.command(name="list")
@click.option(
    "--scope",
    type=click.Choice(["user", "project", "local"], case_sensitive=False),
    help="Config scope: user (~/.mng/profiles/<profile_id>/), project (.mng/), or local (.mng/settings.local.toml)",
)
@add_common_options
@click.pass_context
def config_list(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _config_list_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _config_list_impl(ctx: click.Context, **kwargs: Any) -> None:
    """Implementation of config list command."""
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
        is_format_template_supported=True,
    )

    root_name = os.environ.get("MNG_ROOT_NAME", "mng")

    if opts.scope:
        # List config from specific scope
        scope = ConfigScope(opts.scope.upper())
        config_path = get_config_path(scope, root_name, mng_ctx.profile_dir, mng_ctx.concurrency_group)
        config_data = _load_config_file(config_path)
        _emit_config_list(config_data, output_opts, scope, config_path)
    else:
        # List merged config (show what's currently in effect)
        config_data = mng_ctx.config.model_dump(mode="json")
        _emit_config_list(config_data, output_opts, None, None)


def _emit_config_list(
    config_data: dict[str, Any],
    output_opts: OutputOptions,
    scope: ConfigScope | None,
    config_path: Path | None,
) -> None:
    """Emit the config list output in the appropriate format."""
    if output_opts.format_template is not None:
        flattened = _flatten_config(config_data)
        items = [{"key": key, "value": _format_value_for_display(value)} for key, value in sorted(flattened)]
        emit_format_template_lines(output_opts.format_template, items)
        return
    match output_opts.output_format:
        case OutputFormat.JSON:
            output = {"config": config_data}
            if scope is not None:
                output["scope"] = scope.value.lower()
            if config_path is not None:
                output["path"] = str(config_path)
            emit_final_json(output)
        case OutputFormat.JSONL:
            output = {"event": "config_list", "config": config_data}
            if scope is not None:
                output["scope"] = scope.value.lower()
            if config_path is not None:
                output["path"] = str(config_path)
            emit_final_json(output)
        case OutputFormat.HUMAN:
            if scope is not None and config_path is not None:
                write_human_line("Config from {} ({}):", scope.value.lower(), config_path)
            else:
                write_human_line("Merged configuration (all scopes):")
            write_human_line("")
            if not config_data:
                write_human_line("  (empty)")
            else:
                flattened = _flatten_config(config_data)
                for key, value in sorted(flattened):
                    write_human_line("  {} = {}", key, _format_value_for_display(value))
        case _ as unreachable:
            assert_never(unreachable)


@config.command(name="get")
@click.argument("key")
@click.option(
    "--scope",
    type=click.Choice(["user", "project", "local"], case_sensitive=False),
    help="Config scope: user (~/.mng/profiles/<profile_id>/), project (.mng/), or local (.mng/settings.local.toml)",
)
@add_common_options
@click.pass_context
def config_get(ctx: click.Context, key: str, **kwargs: Any) -> None:
    try:
        _config_get_impl(ctx, key, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _config_get_impl(ctx: click.Context, key: str, **kwargs: Any) -> None:
    """Implementation of config get command."""
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
    )

    root_name = os.environ.get("MNG_ROOT_NAME", "mng")

    if opts.scope:
        # Get from specific scope
        scope = ConfigScope(opts.scope.upper())
        config_path = get_config_path(scope, root_name, mng_ctx.profile_dir, mng_ctx.concurrency_group)
        config_data = _load_config_file(config_path)
    else:
        # Get from merged config
        config_data = mng_ctx.config.model_dump(mode="json")

    try:
        value = _get_nested_value(config_data, key)
        _emit_config_value(key, value, output_opts)
    except KeyError:
        _emit_key_not_found(key, output_opts)
        ctx.exit(1)


def _emit_config_value(key: str, value: Any, output_opts: OutputOptions) -> None:
    """Emit a config value in the appropriate format."""
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json({"key": key, "value": value})
        case OutputFormat.JSONL:
            emit_final_json({"event": "config_value", "key": key, "value": value})
        case OutputFormat.HUMAN:
            write_human_line("{}", _format_value_for_display(value))
        case _ as unreachable:
            assert_never(unreachable)


def _emit_key_not_found(key: str, output_opts: OutputOptions) -> None:
    """Emit a key not found error in the appropriate format."""
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json({"error": f"Key not found: {key}", "key": key})
        case OutputFormat.JSONL:
            emit_final_json({"event": "error", "message": f"Key not found: {key}", "key": key})
        case OutputFormat.HUMAN:
            logger.error("Key not found: {}", key)
        case _ as unreachable:
            assert_never(unreachable)


@config.command(name="set")
@click.argument("key")
@click.argument("value")
@click.option(
    "--scope",
    type=click.Choice(["user", "project", "local"], case_sensitive=False),
    default="project",
    show_default=True,
    help="Config scope: user (~/.mng/profiles/<profile_id>/), project (.mng/), or local (.mng/settings.local.toml)",
)
@add_common_options
@click.pass_context
def config_set(ctx: click.Context, key: str, value: str, **kwargs: Any) -> None:
    try:
        _config_set_impl(ctx, key, value, **kwargs)
    except ConfigParseError as e:
        logger.error("Invalid configuration: {}", e)
        ctx.exit(1)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _config_set_impl(ctx: click.Context, key: str, value: str, **kwargs: Any) -> None:
    """Implementation of config set command."""
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
    )

    root_name = os.environ.get("MNG_ROOT_NAME", "mng")
    scope = ConfigScope((opts.scope or "project").upper())
    config_path = get_config_path(scope, root_name, mng_ctx.profile_dir, mng_ctx.concurrency_group)

    # Load existing config
    doc = load_config_file_tomlkit(config_path)

    # Parse and set the value
    parsed_value = _parse_value(value)
    set_nested_value(doc, key, parsed_value)

    # Validate the resulting config before saving
    parse_config(
        dict(doc.unwrap()),
        disabled_plugins=mng_ctx.config.disabled_plugins,
    )

    # Save the config
    save_config_file(config_path, doc)

    _emit_config_set_result(key, parsed_value, scope, config_path, output_opts)


def _emit_config_set_result(
    key: str,
    value: Any,
    scope: ConfigScope,
    config_path: Path,
    output_opts: OutputOptions,
) -> None:
    """Emit the result of a config set operation."""
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json(
                {
                    "key": key,
                    "value": value,
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.JSONL:
            emit_final_json(
                {
                    "event": "config_set",
                    "key": key,
                    "value": value,
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.HUMAN:
            write_human_line(
                "Set {} = {} in {} ({})", key, _format_value_for_display(value), scope.value.lower(), config_path
            )
        case _ as unreachable:
            assert_never(unreachable)


@config.command(name="unset")
@click.argument("key")
@click.option(
    "--scope",
    type=click.Choice(["user", "project", "local"], case_sensitive=False),
    default="project",
    show_default=True,
    help="Config scope: user (~/.mng/profiles/<profile_id>/), project (.mng/), or local (.mng/settings.local.toml)",
)
@add_common_options
@click.pass_context
def config_unset(ctx: click.Context, key: str, **kwargs: Any) -> None:
    try:
        _config_unset_impl(ctx, key, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _config_unset_impl(ctx: click.Context, key: str, **kwargs: Any) -> None:
    """Implementation of config unset command."""
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
    )

    root_name = os.environ.get("MNG_ROOT_NAME", "mng")
    scope = ConfigScope((opts.scope or "project").upper())
    config_path = get_config_path(scope, root_name, mng_ctx.profile_dir, mng_ctx.concurrency_group)

    if not config_path.exists():
        _emit_key_not_found(key, output_opts)
        ctx.exit(1)

    # Load existing config
    doc = load_config_file_tomlkit(config_path)

    # Remove the value
    if _unset_nested_value(doc, key):
        # Save the config
        save_config_file(config_path, doc)
        _emit_config_unset_result(key, scope, config_path, output_opts)
    else:
        _emit_key_not_found(key, output_opts)
        ctx.exit(1)


def _emit_config_unset_result(
    key: str,
    scope: ConfigScope,
    config_path: Path,
    output_opts: OutputOptions,
) -> None:
    """Emit the result of a config unset operation."""
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json(
                {
                    "key": key,
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.JSONL:
            emit_final_json(
                {
                    "event": "config_unset",
                    "key": key,
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.HUMAN:
            write_human_line("Removed {} from {} ({})", key, scope.value.lower(), config_path)
        case _ as unreachable:
            assert_never(unreachable)


@config.command(name="edit")
@click.option(
    "--scope",
    type=click.Choice(["user", "project", "local"], case_sensitive=False),
    default="project",
    show_default=True,
    help="Config scope: user (~/.mng/profiles/<profile_id>/), project (.mng/), or local (.mng/settings.local.toml)",
)
@add_common_options
@click.pass_context
def config_edit(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _config_edit_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _config_edit_impl(ctx: click.Context, **kwargs: Any) -> None:
    """Implementation of config edit command."""
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
    )

    root_name = os.environ.get("MNG_ROOT_NAME", "mng")
    scope = ConfigScope((opts.scope or "project").upper())
    config_path = get_config_path(scope, root_name, mng_ctx.profile_dir, mng_ctx.concurrency_group)

    # Create the config file if it doesn't exist
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(config_path, _get_config_template())

    # Get the editor
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"

    match output_opts.output_format:
        case OutputFormat.HUMAN:
            write_human_line("Opening {} in {}...", config_path, editor)
        case OutputFormat.JSON | OutputFormat.JSONL:
            pass
        case _ as unreachable:
            assert_never(unreachable)

    # Open the editor
    try:
        run_interactive_subprocess([editor, str(config_path)], check=True)
    except subprocess.CalledProcessError as e:
        logger.error("Editor exited with error: {}", e.returncode)
        ctx.exit(e.returncode)
    except FileNotFoundError:
        logger.error("Editor not found: {}", editor)
        logger.error("Set $EDITOR or $VISUAL environment variable to your preferred editor")
        ctx.exit(1)

    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json(
                {
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.JSONL:
            emit_final_json(
                {
                    "event": "config_edited",
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.HUMAN:
            pass
        case _ as unreachable:
            assert_never(unreachable)


def _get_config_template() -> str:
    """Get a template for a new config file."""
    return """# mng configuration file
# See 'mng help --config' for available options

# Resource naming prefix
# prefix = "mng-"

# Default host directory
# default_host_dir = "~/.mng"

# Custom agent types
# [agent_types.my_claude]
# parent_type = "claude"
# cli_args = "--env CLAUDE_MODEL=opus"
# permissions = ["github", "npm"]

# Provider instances
# [providers.my-docker]
# backend = "docker"

# Command defaults
# [commands.create]
# new_branch_prefix = "agent/"
# connect = false

# Logging configuration
# [logging]
# console_level = "INFO"
# file_level = "DEBUG"
"""


@config.command(name="path")
@click.option(
    "--scope",
    type=click.Choice(["user", "project", "local"], case_sensitive=False),
    help="Config scope: user (~/.mng/profiles/<profile_id>/), project (.mng/), or local (.mng/settings.local.toml)",
)
@add_common_options
@click.pass_context
def config_path(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _config_path_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _config_path_impl(ctx: click.Context, **kwargs: Any) -> None:
    """Implementation of config path command."""
    mng_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
    )

    root_name = os.environ.get("MNG_ROOT_NAME", "mng")

    if opts.scope:
        # Show specific scope
        scope = ConfigScope(opts.scope.upper())
        try:
            config_path = get_config_path(scope, root_name, mng_ctx.profile_dir, mng_ctx.concurrency_group)
            _emit_single_path(scope, config_path, output_opts)
        except ConfigNotFoundError as e:
            match output_opts.output_format:
                case OutputFormat.JSON:
                    emit_final_json({"error": str(e), "scope": scope.value.lower()})
                case OutputFormat.JSONL:
                    emit_final_json({"event": "error", "message": str(e), "scope": scope.value.lower()})
                case OutputFormat.HUMAN:
                    logger.error("{}", e)
                case _ as unreachable:
                    assert_never(unreachable)
            ctx.exit(1)
    else:
        # Show all scopes
        paths: list[dict[str, Any]] = []
        for scope in ConfigScope:
            try:
                config_path = get_config_path(scope, root_name, mng_ctx.profile_dir, mng_ctx.concurrency_group)
                paths.append(
                    {
                        "scope": scope.value.lower(),
                        "path": str(config_path),
                        "exists": config_path.exists(),
                    }
                )
            except ConfigNotFoundError:
                paths.append(
                    {
                        "scope": scope.value.lower(),
                        "path": None,
                        "exists": False,
                        "error": f"No git repository found for {scope.value.lower()} config",
                    }
                )
        _emit_all_paths(paths, output_opts)


def _emit_single_path(scope: ConfigScope, config_path: Path, output_opts: OutputOptions) -> None:
    """Emit a single config path."""
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json(
                {
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                    "exists": config_path.exists(),
                }
            )
        case OutputFormat.JSONL:
            emit_final_json(
                {
                    "event": "config_path",
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                    "exists": config_path.exists(),
                }
            )
        case OutputFormat.HUMAN:
            write_human_line("{}", config_path)
        case _ as unreachable:
            assert_never(unreachable)


def _emit_all_paths(paths: list[dict[str, Any]], output_opts: OutputOptions) -> None:
    """Emit all config paths."""
    match output_opts.output_format:
        case OutputFormat.JSON:
            emit_final_json({"paths": paths})
        case OutputFormat.JSONL:
            emit_final_json({"event": "config_paths", "paths": paths})
        case OutputFormat.HUMAN:
            for path_info in paths:
                scope = path_info["scope"]
                path = path_info.get("path")
                exists = path_info.get("exists", False)
                if path:
                    status = "exists" if exists else "not found"
                    write_human_line("{}: {} ({})", scope, path, status)
                else:
                    error = path_info.get("error", "unavailable")
                    write_human_line("{}: {}", scope, error)
        case _ as unreachable:
            assert_never(unreachable)


# Register help metadata for git-style help formatting
CommandHelpMetadata(
    key="config",
    one_line_description="Manage mng configuration",
    synopsis="mng [config|cfg] <subcommand> [OPTIONS]",
    description="""View, edit, and modify mng configuration settings at the user, project, or
local level. Much like a simpler version of `git config`, this command allows
you to manage configuration settings at different scopes.

Configuration is stored in TOML files:
- User: ~/.mng/settings.toml
- Project: .mng/settings.toml (in your git root)
- Local: .mng/settings.local.toml (git-ignored, for local overrides)""",
    aliases=("cfg",),
    examples=(
        ("List all configuration values", "mng config list"),
        ("Get a specific value", "mng config get provider.docker.image"),
        ("Set a value at user scope", "mng config set --user provider.docker.image my-image:latest"),
        ("Edit config in your editor", "mng config edit"),
        ("Show config file paths", "mng config path"),
    ),
    see_also=(("create", "Create a new agent with configuration"),),
).register()

add_pager_help_option(config)

# -- Subcommand help metadata --

CommandHelpMetadata(
    key="config.list",
    one_line_description="List all configuration values",
    synopsis="mng config list [OPTIONS]",
    description="""Shows all configuration settings from the specified scope, or from the
merged configuration if no scope is specified.

Supports custom format templates via --format. Available fields:
key, value.""",
    examples=(
        ("List merged configuration", "mng config list"),
        ("List user-scope configuration", "mng config list --scope user"),
        ("Output as JSON", "mng config list --format json"),
        ("Custom format template", "mng config list --format '{key}={value}'"),
    ),
    see_also=(
        ("config get", "Get a specific configuration value"),
        ("config set", "Set a configuration value"),
    ),
).register()
add_pager_help_option(config_list)

CommandHelpMetadata(
    key="config.get",
    one_line_description="Get a configuration value",
    synopsis="mng config get KEY [OPTIONS]",
    description="""Retrieves the value of a specific configuration key. Use dot notation
for nested keys (e.g., 'commands.create.connect').

By default reads from the merged configuration. Use --scope to read
from a specific scope.""",
    examples=(
        ("Get a top-level key", "mng config get prefix"),
        ("Get a nested key", "mng config get commands.create.connect"),
        ("Get from a specific scope", "mng config get logging.console_level --scope user"),
    ),
    see_also=(
        ("config set", "Set a configuration value"),
        ("config list", "List all configuration values"),
    ),
).register()
add_pager_help_option(config_get)

CommandHelpMetadata(
    key="config.set",
    one_line_description="Set a configuration value",
    synopsis="mng config set KEY VALUE [OPTIONS]",
    description="""Sets a configuration value at the specified scope. Use dot notation
for nested keys (e.g., 'commands.create.connect').

Values are parsed as JSON if possible, otherwise as strings.
Use 'true'/'false' for booleans, numbers for integers/floats.""",
    examples=(
        ("Set a string value", 'mng config set prefix "my-"'),
        ("Set a boolean value", "mng config set commands.create.connect false"),
        ("Set at user scope", "mng config set logging.console_level DEBUG --scope user"),
    ),
    see_also=(
        ("config get", "Get a configuration value"),
        ("config unset", "Remove a configuration value"),
    ),
).register()
add_pager_help_option(config_set)

CommandHelpMetadata(
    key="config.unset",
    one_line_description="Remove a configuration value",
    synopsis="mng config unset KEY [OPTIONS]",
    description="""Removes a configuration value from the specified scope. Use dot notation
for nested keys (e.g., 'commands.create.connect').""",
    examples=(
        ("Remove a key from project scope", "mng config unset commands.create.connect"),
        ("Remove a key from user scope", "mng config unset logging.console_level --scope user"),
    ),
    see_also=(
        ("config set", "Set a configuration value"),
        ("config get", "Get a configuration value"),
    ),
).register()
add_pager_help_option(config_unset)

CommandHelpMetadata(
    key="config.edit",
    one_line_description="Open configuration file in editor",
    synopsis="mng config edit [OPTIONS]",
    description="""Opens the configuration file for the specified scope in your default
editor (from $EDITOR or $VISUAL environment variable, or 'vi' as fallback).

If the config file doesn't exist, it will be created with an empty template.""",
    examples=(
        ("Edit project config (default)", "mng config edit"),
        ("Edit user config", "mng config edit --scope user"),
        ("Edit local config", "mng config edit --scope local"),
    ),
    see_also=(
        ("config path", "Show configuration file paths"),
        ("config set", "Set a configuration value"),
    ),
).register()
add_pager_help_option(config_edit)

CommandHelpMetadata(
    key="config.path",
    one_line_description="Show configuration file paths",
    synopsis="mng config path [OPTIONS]",
    description="""Shows the paths to configuration files. If --scope is specified, shows
only that scope's path. Otherwise shows all paths and whether they exist.""",
    examples=(
        ("Show all config file paths", "mng config path"),
        ("Show user config path", "mng config path --scope user"),
    ),
    see_also=(
        ("config edit", "Open configuration file in editor"),
        ("config list", "List all configuration values"),
    ),
).register()
add_pager_help_option(config_path)
