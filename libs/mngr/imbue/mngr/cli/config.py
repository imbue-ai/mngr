import json
import os
import subprocess
import tomllib
from collections.abc import Callable
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
from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.cli.help_formatter import CommandHelpMetadata
from imbue.mngr.cli.help_formatter import add_pager_help_option
from imbue.mngr.cli.help_formatter import show_help_with_pager
from imbue.mngr.cli.output_helpers import AbortError
from imbue.mngr.cli.output_helpers import emit_format_template_lines
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_json_line
from imbue.mngr.cli.urwid_picker import run_single_select_picker
from imbue.mngr.cli.urwid_utils import has_interactive_terminal
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import ConfigScope
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.config.external_settings import MNGR_MERGE_KEY
from imbue.mngr.config.external_settings import OP_SUFFIXES
from imbue.mngr.config.key_resolver import is_settings_overrides_path
from imbue.mngr.config.key_resolver import resolve_extends
from imbue.mngr.config.loader import parse_config
from imbue.mngr.config.pre_readers import get_local_config_path
from imbue.mngr.config.pre_readers import get_project_config_path
from imbue.mngr.config.pre_readers import get_user_config_path
from imbue.mngr.config.pre_readers import resolve_project_config_dir
from imbue.mngr.errors import ConfigKeyNotFoundError
from imbue.mngr.errors import ConfigNotFoundError
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.interactive_subprocess import run_interactive_subprocess
from imbue.mngr.utils.model_schema import render_annotation
from imbue.mngr.utils.model_schema import walk_model_fields
from imbue.mngr.utils.toml_config import load_config_file_tomlkit
from imbue.mngr.utils.toml_config import save_config_file
from imbue.mngr.utils.toml_config import set_nested_value
from imbue.overlay.operators import ASSIGN_SUFFIX
from imbue.overlay.operators import EXTEND_SUFFIX
from imbue.overlay.operators import assign_bare_key
from imbue.overlay.operators import bare_key
from imbue.overlay.operators import is_assign_key
from imbue.overlay.operators import is_extend_key
from imbue.overlay.operators import parse_scalar_value


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
        # Bare key not found; try the ``key__extend`` / ``key__assign`` forms for scope reads.
        for suffix in (EXTEND_SUFFIX, ASSIGN_SUFFIX):
            try:
                suffixed_value = _get_nested_value(config_data, f"{key}{suffix}")
            except KeyError:
                continue
            _emit_config_merge_op_value(key, f"{key}{suffix}", suffixed_value, output_opts)
            return
        _emit_key_not_found(key, output_opts)
        ctx.exit(1)
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


def _emit_config_merge_op_value(key: str, written_key: str, value: Any, output_opts: OutputOptions) -> None:
    """Emit a scope-file ``__extend`` / ``__assign`` key value. Human prints the ellipsis
    sentinel; JSON/JSONL emit the literal TOML key so downstream tooling can round-trip.
    """
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line({"key": written_key, "value": value})
        case OutputFormat.JSONL:
            write_json_line({"event": "config_value", "key": written_key, "value": value})
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
    # No local error handling: a ConfigParseError / UserInputError (the overlay wraps
    # OverlayError into ConfigParseError) is a MngrError rendered by the central CLI handler,
    # and an AbortError is by design a BaseException that propagates to the top level.
    _config_set_impl(ctx, key, value, **kwargs)


def _config_set_impl(ctx: click.Context, key: str, value: str, **kwargs: Any) -> None:
    """Implementation of ``mngr config set``.

    The same path also handles ``mngr config set foo__extend value`` and
    ``foo__assign value`` — when the final segment ends in ``__extend`` /
    ``__assign``, the write is routed through the same code as ``mngr config
    extend`` / ``mngr config assign``, so the spellings are interchangeable.
    """
    last_segment = key.split(".")[-1]
    if is_extend_key(last_segment):
        # ``... .field__extend`` is the same operation as ``mngr config extend``.
        _config_merge_op_impl(ctx, bare_key(key), value, op="extend", **kwargs)
        return
    if is_assign_key(last_segment):
        # ``... .field__assign`` is the same operation as ``mngr config assign``.
        _config_merge_op_impl(ctx, assign_bare_key(key), value, op="assign", **kwargs)
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


def _config_merge_op_impl(ctx: click.Context, key: str, value: str, *, op: str, **kwargs: Any) -> None:
    """Shared implementation of ``mngr config extend`` (``op="extend"``) and
    ``mngr config assign`` (``op="assign"``).

    For a normal config path, writes a ``key__extend`` / ``key__assign`` entry. For a
    ``settings_overrides`` path, the suffixes are not allowed (they would leak into the
    external CLI's settings.json), so it instead writes the bare value and declares the op
    in the root ``__mngr_merge`` map, keyed by the literal dotted path relative to the root
    (re-setting the whole map so an existing directive for another key is preserved). The
    value must be a JSON list / dict / scalar; a structurally invalid target (e.g. an
    ``extend`` on a scalar) raises ``ConfigParseError`` during validation.
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
    key_segments = tuple(key.split("."))
    # len > 3: there is a sub-key under settings_overrides for the __mngr_merge map to target.
    if is_settings_overrides_path(key_segments) and len(key_segments) > 3:
        set_nested_value(doc, key, parsed_value)
        merge_path = f"{'.'.join(key_segments[:3])}.{MNGR_MERGE_KEY}"
        relative = ".".join(key_segments[3:])
        try:
            existing_directives = _get_nested_value(dict(doc.unwrap()), merge_path)
        except ConfigKeyNotFoundError:
            existing_directives = {}
        directives = {**existing_directives, relative: op} if isinstance(existing_directives, dict) else {relative: op}
        set_nested_value(doc, merge_path, directives)
        written_key = f"{merge_path}.{relative}"
    else:
        written_key = f"{key}{OP_SUFFIXES[op]}"
        set_nested_value(doc, written_key, parsed_value)

    # Validate by resolving the new operator against the current merged config.
    _validate_doc_after_set(doc, mngr_ctx.config, disabled_plugins=mngr_ctx.config.disabled_plugins)

    save_config_file(config_path, doc)
    _emit_config_merge_op_result(key, written_key, parsed_value, scope, config_path, output_opts, op=op)


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
    _config_merge_op_impl(ctx, key, value, op="extend", **kwargs)


@config.command(name="assign")
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
def config_assign(ctx: click.Context, key: str, value: str, **kwargs: Any) -> None:
    """Write a ``key__assign`` entry that replaces the base without the narrowing guard."""
    _config_merge_op_impl(ctx, key, value, op="assign", **kwargs)


def _emit_config_merge_op_result(
    key: str,
    written_key: str,
    value: Any,
    scope: ConfigScope,
    config_path: Path,
    output_opts: OutputOptions,
    *,
    op: str,
) -> None:
    """Emit the result of a ``mngr config extend`` / ``assign`` operation."""
    match output_opts.output_format:
        case OutputFormat.JSON:
            write_json_line(
                {
                    "key": written_key,
                    "value": value,
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.JSONL:
            write_json_line(
                {
                    "event": f"config_{op}",
                    "key": written_key,
                    "value": value,
                    "scope": scope.value.lower(),
                    "path": str(config_path),
                }
            )
        case OutputFormat.HUMAN:
            write_human_line(
                "{} {} with {} in {} ({})",
                "Extended" if op == "extend" else "Assigned",
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

    Recurses into nested ``BaseModel`` fields (via the shared
    ``walk_model_fields``). Stops at the dict level for open-ended
    ``dict[str, Any]`` fields (e.g. ``commands.<cmd>.defaults``); the inner key
    shape is user-extensible and not part of the schema. Each row's ``value`` is
    the current effective value resolved from ``current`` by its dotted key.
    """
    if isinstance(current, BaseModel):
        current_dump: dict[str, Any] = current.model_dump(mode="json")
    elif isinstance(current, dict):
        current_dump = current
    else:
        current_dump = {}
    rows: list[dict[str, Any]] = []
    for key, annotation, description in walk_model_fields(model_class):
        rows.append(
            {
                "key": key,
                "type": render_annotation(annotation),
                "value": _value_at_path(current_dump, key),
                "description": description,
            }
        )
    return rows


def _value_at_path(data: dict[str, Any], dotted_key: str) -> Any:
    """Resolve a dotted ``dotted_key`` against nested ``data``, or None if absent."""
    current: Any = data
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


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


@config.command(name="wizard")
@add_common_options
@click.pass_context
def config_wizard(ctx: click.Context, **kwargs: Any) -> None:
    try:
        _config_wizard_impl(ctx, **kwargs)
    except AbortError as e:
        logger.error("Aborted: {}", e.message)
        ctx.exit(1)


def _config_wizard_impl(ctx: click.Context, **kwargs: Any) -> None:
    """Walk through common one-time user-scope configuration steps.

    Each step short-circuits when its setting is already configured, so
    re-running the wizard (e.g. on a repeated install) only prompts for gaps.
    Run from ``scripts/install.sh`` after the extras step.
    """
    mngr_ctx, _, _ = setup_command_context(
        ctx=ctx,
        command_name="config",
        command_class=ConfigCliOptions,
    )

    root_name = os.environ.get("MNGR_ROOT_NAME", "mngr")
    config_path = get_config_path(ConfigScope.USER, root_name, mngr_ctx.profile_dir, mngr_ctx.concurrency_group)

    write_human_line("mngr config wizard")
    write_human_line("")
    _wizard_claude_config_isolation(config_path)


def _is_claude_agent_type_registered() -> bool:
    """Return True when the ``claude`` agent type is registered (mngr Claude plugin installed)."""
    return "claude" in list_registered_agent_types()


def _get_existing_isolation_setting(raw: dict[str, Any]) -> bool | None:
    """Return the user-config value of agent_types.claude.isolate_local_config_dir, or None."""
    agent_types = raw.get("agent_types")
    if not isinstance(agent_types, dict):
        return None
    claude = agent_types.get("claude")
    if not isinstance(claude, dict):
        return None
    value = claude.get("isolate_local_config_dir")
    return value if isinstance(value, bool) else None


def _prompt_claude_isolation_choice() -> bool | None:
    """Show a 2-option picker. Returns True to isolate, False to share, None if cancelled.

    Caller must check ``has_interactive_terminal()`` first.
    """
    options = [
        "Yes -- give each agent its own Claude config (mngr won't touch your default Claude config)",
        "No -- share your default Claude config "
        "(mngr may write to it, but this is needed for Claude subscriptions on macOS to keep credentials working)",
    ]
    # Default the highlighted option to "No" (share) -- the safer choice that keeps
    # Claude subscription credentials working on macOS.
    idx = run_single_select_picker(
        options=options,
        title="mngr config wizard",
        header_text="Enable config dir isolation for local Claude agents?",
        initial_focus=1,
    )
    if idx is None:
        return None
    return idx == 0


def _wizard_claude_config_isolation(
    config_path: Path,
    *,
    # Dependencies are exposed as keyword arguments so tests can substitute
    # in-memory fakes without monkeypatching module-level callables (mirrors
    # the ``_install_*`` seams in extras.py).
    is_claude_registered_fn: Callable[[], bool] = _is_claude_agent_type_registered,
    is_interactive_fn: Callable[[], bool] = has_interactive_terminal,
    prompt_fn: Callable[[], bool | None] = _prompt_claude_isolation_choice,
) -> None:
    """Prompt whether to isolate the Claude config dir for local agents.

    Only runs when the ``claude`` agent type is registered (the mngr Claude
    plugin is installed); config-dir isolation is meaningless otherwise. Writes
    ``agent_types.claude.isolate_local_config_dir`` to the user-scope config.
    Skips silently if the setting is already present in that file.
    """
    if not is_claude_registered_fn():
        return

    existing = _load_config_file(config_path)
    current = _get_existing_isolation_setting(existing)
    if current is not None:
        write_human_line(
            "Claude config dir isolation is already set to {} in {}; skipping.",
            str(current).lower(),
            config_path,
        )
        return

    if not is_interactive_fn():
        write_human_line("No interactive terminal; skipping Claude config dir isolation setup.")
        write_human_line("To set it later, run:")
        write_human_line("    mngr config set agent_types.claude.isolate_local_config_dir <true|false> --scope user")
        return

    choice = prompt_fn()
    if choice is None:
        write_human_line("Skipping Claude config dir isolation.")
        return

    doc = load_config_file_tomlkit(config_path)
    set_nested_value(doc, "agent_types.claude.isolate_local_config_dir", choice)
    save_config_file(config_path, doc)
    write_human_line("Set agent_types.claude.isolate_local_config_dir = {} in {}", str(choice).lower(), config_path)


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
    key="config.assign",
    one_line_description="Assign a value, replacing the base without the narrowing guard",
    synopsis="mngr config assign KEY VALUE [OPTIONS]",
    description="""Writes a ``KEY__assign`` entry into the TOML file. Like a bare
``mngr config set``, the value replaces whatever lower-precedence layers provided -- but
``__assign`` suppresses the narrowing guard, so it will not error when the replacement
drops a non-empty list/dict/set from a lower layer. Use it when you intend to replace an
aggregate wholesale.

On a ``settings_overrides`` path the suffix is not written (Claude would not understand
it); instead the value is written bare plus a ``__mngr_merge`` ``assign`` directive. For
consistency, ``mngr config set KEY__assign VALUE`` routes through this same code path.""",
    examples=(
        (
            "Replace a custom agent type's allow-list (no narrowing error)",
            'mngr config assign agent_types.write-plus.settings_overrides.permissions.allow \'["Read", "Edit"]\'',
        ),
    ),
    see_also=(
        ("config extend", "Extend a list/dict/set value (appends/merges instead of replacing)"),
        ("config set", "Assign a value with the narrowing guard"),
    ),
).register()
add_pager_help_option(config_assign)

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

CommandHelpMetadata(
    key="config.wizard",
    one_line_description="Interactively set up common user-scope configuration",
    synopsis="mngr config wizard [OPTIONS]",
    description="""Walks through common one-time configuration steps, writing to the
user-scope config. Each step short-circuits when its setting is already
configured, so re-running only prompts for what is still unset. Run
automatically by the installer.

Steps:
  Claude config dir isolation  Whether each local Claude agent gets its own
                               config dir (mngr leaves your default Claude
                               config untouched) or shares your default config
                               (needed for Claude subscriptions on macOS).""",
    examples=(("Run the configuration wizard", "mngr config wizard"),),
    see_also=(("config set", "Set a configuration value directly"),),
).register()
add_pager_help_option(config_wizard)
