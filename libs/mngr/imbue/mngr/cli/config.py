import json
import os
import subprocess
import tomllib
import typing
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any
from typing import assert_never
from typing import cast

import click
import tomlkit
from loguru import logger
from pydantic import BaseModel

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.help_formatter import show_help_with_pager
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import ConfigScope
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.config.key_resolver import EXTEND_SUFFIX
from imbue.mngr.config.key_resolver import is_extend_key
from imbue.mngr.config.key_resolver import parse_scalar_value
from imbue.mngr.config.key_resolver import resolve_extends
from imbue.mngr.config.loader import parse_config
from imbue.mngr.config.pre_readers import get_local_config_path
from imbue.mngr.config.pre_readers import get_project_config_path
from imbue.mngr.config.pre_readers import get_user_config_path
from imbue.mngr.config.pre_readers import resolve_project_config_dir
from imbue.mngr.errors import ConfigKeyNotFoundError
from imbue.mngr.errors import ConfigNotFoundError
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.interactive_subprocess import run_interactive_subprocess
from imbue.mngr.utils.toml_config import load_config_file_tomlkit
from imbue.mngr.utils.toml_config import save_config_file
from imbue.mngr.utils.toml_config import set_nested_value


class ConfigCliOptions(CommonCliOptions):
    """Options passed from the CLI to the config command.

    Inherits common options (output_format, quiet, verbose, etc.) from CommonCliOptions.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the click.option() and click.argument() decorators on the config() function itself.
    """

    # ``scope`` is optional because ``mngr config list --schema`` doesn't accept a
    # --scope flag (so click never populates ``ctx.params['scope']`` for it);
    # other subcommands either default it (config_set) or leave it as None.
    scope: str | None = None
    # Arguments used by subcommands (get, set, unset)
    key: str | None = None
    value: str | None = None
    # ``mngr config list --all`` opts into the full-schema listing.
    all: bool = False
    # ``mngr config list --schema`` switches the output to the type-annotated
    # schema view (every settable key with its declared type and description).
    # Bound through the click option's ``"schema_view"`` ctx-params key because
    # the bare name ``schema`` collides with pydantic's deprecated ``.schema``
    # alias on ``BaseModel``.
    schema_view: bool = False


def get_config_path(scope: ConfigScope, root_name: str, profile_dir: Path, cg: ConcurrencyGroup) -> Path:
    """Get the config file path for the given scope. The profile_dir is required for USER scope."""
    match scope:
        case ConfigScope.USER:
            if profile_dir is None:
                raise ConfigNotFoundError("profile_dir is required for USER scope")
            return get_user_config_path(profile_dir)
        case ConfigScope.PROJECT:
            project_dir = resolve_project_config_dir(root_name, cg)
            if project_dir is None:
                raise ConfigNotFoundError("No git repository found for project config")
            return get_project_config_path(project_dir)
        case ConfigScope.LOCAL:
            project_dir = resolve_project_config_dir(root_name, cg)
            if project_dir is None:
                raise ConfigNotFoundError("No git repository found for local config")
            return get_local_config_path(project_dir)
        case _ as unreachable:
            assert_never(unreachable)


def _load_config_file(path: Path) -> dict[str, Any]:
    """Load a TOML config file."""
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _get_nested_value(data: dict[str, Any], key_path: str) -> Any:
    """Get a value from nested dict using dot-separated key path."""
    keys = key_path.split(".")
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise ConfigKeyNotFoundError(key_path)
        current = current[key]
    return current


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
    help="Config scope: user (~/.mngr/profiles/<profile_id>/), project (.mngr/), or local (.mngr/settings.local.toml)",
)
@add_common_options
@click.pass_context
def config(ctx: click.Context, **kwargs: Any) -> None:
    if ctx.invoked_subcommand is None:
        mngr_ctx, _, _ = setup_command_context(
            ctx=ctx,
            command_name="config",
            command_class=ConfigCliOptions,
        )
        show_help_with_pager(ctx, ctx.command, mngr_ctx.config)


@config.command(name="list")
@click.option(
    "--scope",
    type=click.Choice(["user", "project", "local"], case_sensitive=False),
    help="Config scope: user (~/.mngr/profiles/<profile_id>/), project (.mngr/), or local (.mngr/settings.local.toml)",
)
@click.option(
    "--all",
    "all",
    is_flag=True,
    default=False,
    help="Include all settable fields (with their current effective values), not just keys explicitly set in config.",
)
@click.option(
    "--schema",
    "schema_view",
    is_flag=True,
    default=False,
    help="Render each settable key with its declared type and description (the schema view). "
    "Useful for discovering what is settable via MNGR__* env vars, --setting, or mngr config set.",
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
    """Implementation of config list command.

    Default: merged config restricted to keys that appear in at least one of
    the user / project / local TOML scopes. Keys defaulted by ``MngrConfig``
    (or set via ``MNGR__*`` env vars / ``--setting`` for this invocation only)
    are omitted, matching the user expectation that ``list`` reflects what's
    persisted in config files.

    ``--all`` switches to the full ``model_dump`` view so users can discover
    every settable key with its current effective value.

    ``--schema`` walks ``MngrConfig.model_fields`` recursively (through any
    enabled plugin sub-configs) and emits each settable key path alongside
    its declared type and description. Composes naturally with ``--scope``-
    less invocations; ``--scope`` is rejected because schema is independent
    of which TOML file the keys came from.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
        is_format_template_supported=True,
    )

    root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")

    if opts.schema_view:
        if opts.scope:
            raise click.UsageError("--schema and --scope cannot be combined; the schema is global.")
        rows = _collect_schema_rows(MngrConfig, mngr_ctx.config)
        _emit_schema_rows(rows, output_opts)
        return

    if opts.scope:
        # List config from specific scope
        scope = ConfigScope(opts.scope.upper())
        config_path = get_config_path(scope, root_name, mngr_ctx.profile_dir, mngr_ctx.concurrency_group)
        config_data = _load_config_file(config_path)
        _emit_config_list(config_data, output_opts, scope, config_path)
    else:
        # serialize_as_any preserves provider-subclass fields (e.g. docker_runtime
        # on DockerProviderConfig); without it model_dump serializes providers by
        # the declared base type and silently drops subclass-only keys.
        full_view = mngr_ctx.config.model_dump(mode="json", serialize_as_any=True)
        if opts.all:
            config_data = full_view
        else:
            explicit_keys = _collect_explicit_toml_keys(
                root_name,
                mngr_ctx.profile_dir,
                mngr_ctx.concurrency_group,
            )
            config_data = _filter_to_explicit_keys(full_view, explicit_keys)
        _emit_config_list(config_data, output_opts, None, None)


def _collect_explicit_toml_keys(
    root_name: str,
    profile_dir: Path,
    cg: ConcurrencyGroup,
) -> set[tuple[str, ...]]:
    """Return the set of flattened key paths set in any user/project/local TOML scope.

    Used by ``mngr config list`` (without ``--all``) to filter the merged
    config view to keys the user has actually written to a config file.
    Missing scope files (no path resolvable, file absent) contribute no
    keys; a malformed TOML file surfaces as a ``tomllib.TOMLDecodeError``
    from ``_load_config_file``, matching how the rest of ``mngr config``
    handles parse failures.
    """
    explicit: set[tuple[str, ...]] = set()
    for scope in (ConfigScope.USER, ConfigScope.PROJECT, ConfigScope.LOCAL):
        try:
            config_path = get_config_path(scope, root_name, profile_dir, cg)
        except ConfigNotFoundError:
            # E.g. local scope outside a git repo; nothing to contribute.
            continue
        raw = _load_config_file(config_path)
        _collect_key_paths(raw, prefix=(), out=explicit)
    return explicit


def _collect_key_paths(
    data: dict[str, Any],
    prefix: tuple[str, ...],
    out: set[tuple[str, ...]],
) -> None:
    """Walk ``data`` and add a tuple key-path for every leaf key into ``out``.

    A leaf is any value that is not a dict (so nested tables recurse). The
    operator suffix ``__extend`` is leaf-only by spec, so the strip only
    applies to leaf keys; intermediate keys are recorded verbatim so a
    malformed config with ``foo__extend`` on a sub-table doesn't get silently
    collapsed under a ``foo`` prefix.
    """
    for key, value in data.items():
        if isinstance(value, dict):
            _collect_key_paths(value, prefix + (key,), out)
            continue
        bare = key[: -len(EXTEND_SUFFIX)] if isinstance(key, str) and key.endswith(EXTEND_SUFFIX) else key
        out.add(prefix + (bare,))


def _filter_to_explicit_keys(
    config_data: dict[str, Any],
    explicit_keys: set[tuple[str, ...]],
) -> dict[str, Any]:
    """Project ``config_data`` down to only the keys present in ``explicit_keys``.

    Intermediate container dicts are retained when at least one descendant
    key is explicit; empty branches are dropped.
    """
    return _filter_branch(config_data, prefix=(), explicit_keys=explicit_keys)


def _filter_branch(
    data: dict[str, Any],
    prefix: tuple[str, ...],
    explicit_keys: set[tuple[str, ...]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        path = prefix + (key,)
        if isinstance(value, dict):
            filtered = _filter_branch(value, path, explicit_keys)
            if filtered:
                result[key] = filtered
        elif path in explicit_keys:
            result[key] = value
        else:
            # Leaf value at a path the user never wrote to a TOML file; drop it.
            continue
    return result


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
            output: dict[str, object] = {"config": config_data}
            if scope is not None:
                output["scope"] = scope.value.lower()
            if config_path is not None:
                output["path"] = str(config_path)
            write_json_line(output)
        case OutputFormat.JSONL:
            output_jsonl: dict[str, object] = {"event": "config_list", "config": config_data}
            if scope is not None:
                output_jsonl["scope"] = scope.value.lower()
            if config_path is not None:
                output_jsonl["path"] = str(config_path)
            write_json_line(output_jsonl)
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
    help="Config scope: user (~/.mngr/profiles/<profile_id>/), project (.mngr/), or local (.mngr/settings.local.toml)",
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
    """Implementation of config get command.

    Scope mode reads the TOML file literally, so a ``key__extend`` entry is
    rendered with an ellipsis sentinel in human format and as the literal
    TOML key in JSON/JSONL — making it visually obvious that the value is
    an extend operation, not a full assignment.

    Merged mode returns the resolved value (extends are applied before the
    merge completes); the ellipsis sentinel never appears there.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
    )

    root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")

    if opts.scope:
        # Get from specific scope
        scope = ConfigScope(opts.scope.upper())
        config_path = get_config_path(scope, root_name, mngr_ctx.profile_dir, mngr_ctx.concurrency_group)
        config_data = _load_config_file(config_path)
        try:
            value = _get_nested_value(config_data, key)
            _emit_config_value(key, value, output_opts)
            return
        except KeyError:
            pass
        # Bare key not found; try the ``key__extend`` form for scope-file reads.
        extend_key = f"{key}{EXTEND_SUFFIX}"
        try:
            extend_value = _get_nested_value(config_data, extend_key)
        except KeyError:
            _emit_key_not_found(key, output_opts)
            ctx.exit(1)
            return
        _emit_config_extend_value(key, extend_key, extend_value, output_opts)
        return

    # Merged mode: extends are already applied; bare key lookup is sufficient.
    # serialize_as_any preserves provider-subclass fields (e.g. docker_runtime on
    # DockerProviderConfig); without it model_dump serializes providers by the
    # declared base type and silently drops subclass-only keys.
    config_data = mngr_ctx.config.model_dump(mode="json", serialize_as_any=True)
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
            write_json_line({"key": key, "value": value})
        case OutputFormat.JSONL:
            write_json_line({"event": "config_value", "key": key, "value": value})
        case OutputFormat.HUMAN:
            write_human_line("{}", _format_value_for_display(value))
        case _ as unreachable:
            assert_never(unreachable)


def _format_extend_sentinel(value: Any) -> str:
    """Render an extend value with a ``...`` sentinel indicating "extends base".

    Lists become ``[..., item1, item2]`` and dicts become ``{..., k1: v1}``.
    Other shapes fall back to the bare display form (the resolver would have
    rejected them, but be defensive).
    """
    if isinstance(value, list):
        rendered_items = ", ".join(json.dumps(item) for item in value)
        return f"[..., {rendered_items}]" if rendered_items else "[...]"
    if isinstance(value, dict):
        rendered_items = ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in value.items())
        return f"{{..., {rendered_items}}}" if rendered_items else "{...}"
    return _format_value_for_display(value)


def _emit_config_extend_value(key: str, extend_key: str, value: Any, output_opts: OutputOptions) -> None:
    """Emit a scope-file extend-key value. Human prints the ellipsis sentinel;
    JSON/JSONL emit the literal TOML key so downstream tooling can round-trip.
    """
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line({"key": extend_key, "value": value})
        case OutputFormat.JSONL:
            write_json_line({"event": "config_value", "key": extend_key, "value": value})
        case OutputFormat.HUMAN:
            write_human_line("{}", _format_extend_sentinel(value))
        case _ as unreachable:
            assert_never(unreachable)


def _emit_key_not_found(key: str, output_opts: OutputOptions) -> None:
    """Emit a key not found error in the appropriate format."""
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line({"error": f"Key not found: {key}", "key": key})
        case OutputFormat.JSONL:
            write_json_line({"event": "error", "message": f"Key not found: {key}", "key": key})
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
    help="Config scope: user (~/.mngr/profiles/<profile_id>/), project (.mngr/), or local (.mngr/settings.local.toml)",
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
    """Implementation of ``mngr config set``.

    The same path also handles ``mngr config set foo__extend value`` — when
    the final segment of the key ends in ``__extend``, the write is routed
    through the same code as ``mngr config extend foo value``, so the two
    spellings are interchangeable.
    """
    if is_extend_key(key.split(".")[-1]):
        # ``... .field__extend`` is the same operation as ``mngr config extend``.
        bare = key[: -len(EXTEND_SUFFIX)]
        _config_extend_impl(ctx, bare, value, **kwargs)
        return

    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
    )

    root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")
    scope = ConfigScope((opts.scope or "project").upper())
    config_path = get_config_path(scope, root_name, mngr_ctx.profile_dir, mngr_ctx.concurrency_group)

    # Load existing config
    doc = load_config_file_tomlkit(config_path)

    # Parse and set the value
    parsed_value = parse_scalar_value(value)
    set_nested_value(doc, key, parsed_value)

    # Validate the resulting config (resolving any ``__extend`` keys already in
    # the file against the merged context) before saving.
    _validate_doc_after_set(doc, mngr_ctx.config, disabled_plugins=mngr_ctx.config.disabled_plugins)

    # Save the config
    save_config_file(config_path, doc)

    _emit_config_set_result(key, parsed_value, scope, config_path, output_opts)


def _validate_doc_after_set(doc: Any, base_config: MngrConfig, *, disabled_plugins: frozenset[str]) -> None:
    """Validate a tomlkit document after a ``set`` / ``extend`` mutation.

    Resolves any ``__extend`` keys present in the file against ``base_config``
    so the parser sees only plain assignments, then runs ``parse_config`` in
    strict mode to catch unknown keys / wrong types before the file is saved.
    """
    raw = dict(doc.unwrap())
    resolved = resolve_extends(base_config, raw)
    parse_config(resolved, disabled_plugins=disabled_plugins)


def _config_extend_impl(ctx: click.Context, key: str, value: str, **kwargs: Any) -> None:
    """Implementation of ``mngr config extend``.

    Writes a ``key__extend`` entry into the TOML file. The value must be a
    JSON list / dict / scalar matching the target field's aggregate type;
    a scalar target raises ``ConfigParseError``.
    """
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
    )

    root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")
    scope = ConfigScope((opts.scope or "project").upper())
    config_path = get_config_path(scope, root_name, mngr_ctx.profile_dir, mngr_ctx.concurrency_group)

    doc = load_config_file_tomlkit(config_path)
    parsed_value = parse_scalar_value(value)
    extend_key = f"{key}{EXTEND_SUFFIX}"
    set_nested_value(doc, extend_key, parsed_value)

    # Validate by resolving the new extend against the current merged config.
    _validate_doc_after_set(doc, mngr_ctx.config, disabled_plugins=mngr_ctx.config.disabled_plugins)

    save_config_file(config_path, doc)
    _emit_config_extend_result(key, extend_key, parsed_value, scope, config_path, output_opts)


@config.command(name="extend")
@click.argument("key")
@click.argument("value")
@click.option(
    "--scope",
    type=click.Choice(["user", "project", "local"], case_sensitive=False),
    default="project",
    show_default=True,
    help="Config scope: user (~/.mngr/profiles/<profile_id>/), project (.mngr/), or local (.mngr/settings.local.toml)",
)
@add_common_options
@click.pass_context
def config_extend(ctx: click.Context, key: str, value: str, **kwargs: Any) -> None:
    """Write a ``key__extend`` entry that appends to / merges with the base."""
    try:
        _config_extend_impl(ctx, key, value, **kwargs)
    except ConfigParseError as e:
        logger.error("Invalid configuration: {}", e)
        ctx.exit(1)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _emit_config_extend_result(
    key: str,
    extend_key: str,
    value: Any,
    scope: ConfigScope,
    config_path: Path,
    output_opts: OutputOptions,
) -> None:
    """Emit the result of a ``mngr config extend`` operation."""
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line(
                {
                    "key": extend_key,
                    "value": value,
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.JSONL:
            write_json_line(
                {
                    "event": "config_extend",
                    "key": extend_key,
                    "value": value,
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.HUMAN:
            write_human_line(
                "Extended {} with {} in {} ({})",
                key,
                _format_value_for_display(value),
                scope.value.lower(),
                config_path,
            )
        case _ as unreachable:
            assert_never(unreachable)


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
            write_json_line(
                {
                    "key": key,
                    "value": value,
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.JSONL:
            write_json_line(
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
    help="Config scope: user (~/.mngr/profiles/<profile_id>/), project (.mngr/), or local (.mngr/settings.local.toml)",
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
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
    )

    root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")
    scope = ConfigScope((opts.scope or "project").upper())
    config_path = get_config_path(scope, root_name, mngr_ctx.profile_dir, mngr_ctx.concurrency_group)

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
            write_json_line(
                {
                    "key": key,
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.JSONL:
            write_json_line(
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
    help="Config scope: user (~/.mngr/profiles/<profile_id>/), project (.mngr/), or local (.mngr/settings.local.toml)",
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
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
    )

    root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")
    scope = ConfigScope((opts.scope or "project").upper())
    config_path = get_config_path(scope, root_name, mngr_ctx.profile_dir, mngr_ctx.concurrency_group)

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
            write_json_line(
                {
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.JSONL:
            write_json_line(
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
    return """# mngr configuration file
# See 'mngr help --config' for available options

# Resource naming prefix
# prefix = "mngr-"

# Default host directory
# default_host_dir = "~/.mngr"

# Custom agent types
# [agent_types.my_claude]
# parent_type = "claude"
# cli_args = "--env CLAUDE_MODEL=opus"

# Provider instances
# [providers.my-docker]
# backend = "docker"

# Command defaults
# [commands.create]
# branch = "main:agent/*"
# connect = false

# Logging configuration
# [logging]
# console_level = "INFO"
# file_level = "DEBUG"
"""


def _collect_schema_rows(model_class: type[BaseModel], current: Any) -> list[dict[str, Any]]:
    """Flatten ``model_class``'s schema into ``[{key, type, value, description}, ...]``.

    Recurses into nested ``BaseModel`` fields. Stops at the dict level for
    open-ended ``dict[str, Any]`` fields (e.g. ``commands.<cmd>.defaults``);
    the inner key shape is user-extensible and not part of the schema.
    """
    rows: list[dict[str, Any]] = []
    _walk_schema(model_class, current, prefix=(), rows=rows)
    return rows


def _walk_schema(
    model_class: type[BaseModel],
    current: Any,
    prefix: tuple[str, ...],
    rows: list[dict[str, Any]],
) -> None:
    # Project the current model to a plain dict once so we can look up dynamic
    # field names via dict-key access (rather than ``getattr``).
    current_as_dict: dict[str, Any] | None
    if isinstance(current, BaseModel):
        current_as_dict = current.model_dump(mode="json")
    elif isinstance(current, dict):
        current_as_dict = current
    else:
        current_as_dict = None
    for field_name, field_info in model_class.model_fields.items():
        path = ".".join(prefix + (field_name,))
        annotation = field_info.annotation
        description = field_info.description or ""
        value = None if current_as_dict is None else current_as_dict.get(field_name)
        # Recurse into nested pydantic models so plugin-defined sub-configs
        # appear too. Container dicts (agent_types, providers, etc.) and
        # leaf dicts both stop at this level — their value shape is not part
        # of the schema enumeration.
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            _walk_schema(annotation, value, prefix=prefix + (field_name,), rows=rows)
            continue
        rows.append(
            {
                "key": path,
                "type": _render_annotation(annotation),
                "value": value,
                "description": description,
            }
        )


def _render_annotation(annotation: Any) -> str:
    """Best-effort string for an annotation.

    Parameterised generics like ``list[str]`` carry a useful type parameter
    that ``__name__`` discards (it would return just ``"list"``), so for any
    annotation that has generic args or origin we use ``repr`` -- which
    renders as ``list[str]``, ``dict[str, Path]``, ``str | None``, etc.
    Plain classes (``str``, ``Path``, ``int``) still use ``__name__`` for the
    short, familiar form.
    """
    if annotation is None:
        return "None"
    if typing.get_args(annotation) or typing.get_origin(annotation) is not None:
        return repr(annotation)
    name = getattr(annotation, "__name__", None)
    if name:
        return name
    return repr(annotation)


def _emit_schema_rows(rows: list[dict[str, Any]], output_opts: OutputOptions) -> None:
    """Emit schema rows in the appropriate format."""
    if output_opts.format_template is not None:
        emit_format_template_lines(output_opts.format_template, rows)
        return
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line({"schema": rows})
        case OutputFormat.JSONL:
            write_json_line({"event": "config_list_schema", "schema": rows})
        case OutputFormat.HUMAN:
            for row in rows:
                write_human_line(
                    "  {} : {} = {}",
                    row["key"],
                    row["type"],
                    _format_value_for_display(row["value"]),
                )
        case _ as unreachable:
            assert_never(unreachable)


@config.command(name="path")
@click.option(
    "--scope",
    type=click.Choice(["user", "project", "local"], case_sensitive=False),
    help="Config scope: user (~/.mngr/profiles/<profile_id>/), project (.mngr/), or local (.mngr/settings.local.toml)",
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
    mngr_ctx, output_opts, opts = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
    )

    root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")

    if opts.scope:
        # Show specific scope
        scope = ConfigScope(opts.scope.upper())
        try:
            config_path = get_config_path(scope, root_name, mngr_ctx.profile_dir, mngr_ctx.concurrency_group)
            _emit_single_path(scope, config_path, output_opts)
        except ConfigNotFoundError as e:
            match output_opts.output_format:
                case OutputFormat.JSON:
                    write_json_line({"error": str(e), "scope": scope.value.lower()})
                case OutputFormat.JSONL:
                    write_json_line({"event": "error", "message": str(e), "scope": scope.value.lower()})
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
                config_path = get_config_path(scope, root_name, mngr_ctx.profile_dir, mngr_ctx.concurrency_group)
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
            write_json_line(
                {
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                    "exists": config_path.exists(),
                }
            )
        case OutputFormat.JSONL:
            write_json_line(
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
            write_json_line({"paths": paths})
        case OutputFormat.JSONL:
            write_json_line({"event": "config_paths", "paths": paths})
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
    one_line_description="Manage mngr configuration",
    synopsis="mngr [config|cfg] <subcommand> [OPTIONS]",
    description="""View, edit, and modify mngr configuration settings at the user, project, or
local level. Much like a simpler version of `git config`, this command allows
you to manage configuration settings at different scopes.

Configuration is stored in TOML files:
- User: ~/.mngr/settings.toml
- Project: .mngr/settings.toml (in your git root)
- Local: .mngr/settings.local.toml (git-ignored, for local overrides)""",
    aliases=("cfg",),
    examples=(
        ("List all configuration values", "mngr config list"),
        ("Get a specific value", "mngr config get provider.docker.image"),
        ("Set a value at user scope", "mngr config set --scope user provider.docker.image my-image:latest"),
        ("Edit config in your editor", "mngr config edit"),
        ("Show config file paths", "mngr config path"),
    ),
    see_also=(("create", "Create a new agent with configuration"),),
).register()

add_pager_help_option(config)

# -- Subcommand help metadata --

CommandHelpMetadata(
    key="config.list",
    one_line_description="List all configuration values",
    synopsis="mngr config list [OPTIONS]",
    description="""Shows all configuration settings from the specified scope, or from the
merged configuration if no scope is specified. By default only keys that
appear in a user/project/local TOML file are listed; use ``--all`` to include
every settable field with its current effective value.

Pass ``--schema`` to render each settable key with its declared type and
description (useful for discovering what is settable via ``MNGR__*`` env vars,
``--setting``, or ``mngr config set``). ``--schema`` cannot be combined with
``--scope``.

Supports custom format templates via --format. Available fields:
key, value (and additionally type, description when ``--schema`` is set).""",
    examples=(
        ("List merged configuration", "mngr config list"),
        ("List every settable field with its current value", "mngr config list --all"),
        ("Print the full schema with types", "mngr config list --schema"),
        ("List user-scope configuration", "mngr config list --scope user"),
        ("Output as JSON", "mngr config list --format json"),
        ("Custom format template", "mngr config list --format '{key}={value}'"),
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
    synopsis="mngr config get KEY [OPTIONS]",
    description="""Retrieves the value of a specific configuration key. Use dot notation
for nested keys (e.g., 'commands.create.connect').

By default reads from the merged configuration. Use --scope to read
from a specific scope.""",
    examples=(
        ("Get a top-level key", "mngr config get prefix"),
        ("Get a nested key", "mngr config get commands.create.connect"),
        ("Get from a specific scope", "mngr config get logging.console_level --scope user"),
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
    synopsis="mngr config set KEY VALUE [OPTIONS]",
    description="""Sets a configuration value at the specified scope. Use dot notation
for nested keys (e.g., 'commands.create.connect').

Values are parsed as JSON if possible, otherwise as strings.
Use 'true'/'false' for booleans, numbers for integers/floats.""",
    examples=(
        ("Set a string value", 'mngr config set prefix "my-"'),
        ("Set a boolean value", "mngr config set commands.create.connect false"),
        ("Set at user scope", "mngr config set logging.console_level DEBUG --scope user"),
    ),
    see_also=(
        ("config get", "Get a configuration value"),
        ("config unset", "Remove a configuration value"),
    ),
).register()
add_pager_help_option(config_set)

CommandHelpMetadata(
    key="config.extend",
    one_line_description="Extend a list/dict/set configuration value",
    synopsis="mngr config extend KEY VALUE [OPTIONS]",
    description="""Writes a ``KEY__extend`` entry into the TOML file. When the
config is loaded, the extend operation is applied on top of whatever the lower
precedence layers provided: lists/tuples are concatenated, dicts shallow-merge
keys, and sets are unioned. The target field must be an aggregate; a scalar
target raises an error.

For consistency, ``mngr config set KEY__extend VALUE`` is also accepted and
routes through this same code path.""",
    examples=(
        (
            "Append a CLI arg to a custom agent type",
            'mngr config extend agent_types.my_claude.cli_args \'["--model", "opus"]\'',
        ),
        ("Add an entry to work_dir_extra_paths", 'mngr config extend work_dir_extra_paths \'{".venv": "SHARE"}\''),
    ),
    see_also=(
        ("config set", "Assign a configuration value (replaces, not appends)"),
        ("config get", "Get a configuration value"),
    ),
).register()
add_pager_help_option(config_extend)

CommandHelpMetadata(
    key="config.unset",
    one_line_description="Remove a configuration value",
    synopsis="mngr config unset KEY [OPTIONS]",
    description="""Removes a configuration value from the specified scope. Use dot notation
for nested keys (e.g., 'commands.create.connect').""",
    examples=(
        ("Remove a key from project scope", "mngr config unset commands.create.connect"),
        ("Remove a key from user scope", "mngr config unset logging.console_level --scope user"),
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
    synopsis="mngr config edit [OPTIONS]",
    description="""Opens the configuration file for the specified scope in your default
editor (from $EDITOR or $VISUAL environment variable, or 'vi' as fallback).

If the config file doesn't exist, it will be created with an empty template.""",
    examples=(
        ("Edit project config (default)", "mngr config edit"),
        ("Edit user config", "mngr config edit --scope user"),
        ("Edit local config", "mngr config edit --scope local"),
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
    synopsis="mngr config path [OPTIONS]",
    description="""Shows the paths to configuration files. If --scope is specified, shows
only that scope's path. Otherwise shows all paths and whether they exist.""",
    examples=(
        ("Show all config file paths", "mngr config path"),
        ("Show user config path", "mngr config path --scope user"),
    ),
    see_also=(
        ("config edit", "Open configuration file in editor"),
        ("config list", "List all configuration values"),
    ),
).register()
add_pager_help_option(config_path)
