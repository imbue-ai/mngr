import json
import types
import typing
from enum import Enum
from typing import Any
from typing import Final
from typing import NamedTuple

import click
from loguru import logger
from pydantic import BaseModel

from imbue.mngr.config.completion_cache import COMPLETION_CACHE_FILENAME
from imbue.mngr.config.completion_cache import CompletionCacheData
from imbue.mngr.config.completion_cache import get_completion_cache_dir
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.provider_config_registry import list_registered_provider_backend_names
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.utils.click_utils import detect_alias_to_canonical
from imbue.mngr.utils.file_utils import atomic_write

# Per-position positional completion spec for top-level commands.
# Maps command name -> list of source identifier lists per position.
# Each inner list contains source names for that position (empty = freeform).
# For variadic commands (nargs=None), the last entry repeats.
# Source identifiers: "agent_names", "host_names", "plugin_names", "config_keys"
_POSITIONAL_COMPLETION_SPEC: Final[dict[str, list[list[str]]]] = {
    "connect": [["agent_names"]],
    "destroy": [["agent_names"]],
    "exec": [["agent_names"]],
    "limit": [["agent_names"]],
    "events": [["agent_names", "host_names"], []],
    "message": [["agent_names"]],
    "pair": [["agent_names"]],
    "provision": [["agent_names"]],
    "pull": [["agent_names"], []],
    "push": [["agent_names"], []],
    "rename": [["agent_names"], []],
    "start": [["agent_names"]],
    "stop": [["agent_names"]],
}

# Per-position positional completion spec for group subcommands.
# Uses dotted notation: "group.subcommand".
_POSITIONAL_COMPLETION_SUBCOMMAND_SPEC: Final[dict[str, list[list[str]]]] = {
    "snapshot.create": [["agent_names"]],
    "snapshot.destroy": [["agent_names"]],
    "snapshot.list": [["agent_names"]],
    "plugin.enable": [["plugin_names"]],
    "plugin.disable": [["plugin_names"]],
    "config.get": [["config_keys"]],
    "config.set": [["config_keys"], ["config_value_for_key"]],
    "config.unset": [["config_keys"]],
}

# Options (keyed as "command.--option") whose values should complete against
# git branch names. The lightweight completer reads this field to decide when
# to offer git branch completions.
_GIT_BRANCH_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "create.--branch",
    }
)

# Options whose values should complete against host names from the discovery
# event stream. Uses the same "command.--option" notation.
_HOST_NAME_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "create.--target",
    }
)

# Click option names (--long forms) that should complete against plugin names.
_PLUGIN_NAME_OPTION_NAMES: Final[frozenset[str]] = frozenset(
    {
        "--plugin",
        "--enable-plugin",
        "--disable-plugin",
    }
)

# Config key prefixes to exclude from tab completion. These are derived or
# computed fields that are not meaningful to set directly via `mngr config set`.
_EXCLUDED_CONFIG_KEY_PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "disabled_plugins",
    }
)

# Options that receive dynamic choice values from runtime context (config,
# registries). Maps "command.--option" to the key in dynamic_completions.
_DYNAMIC_CHOICE_OPTIONS: Final[dict[str, str]] = {
    "create.--type": "agent_type_names",
    "create.--template": "template_names",
    "create.--provider": "provider_names",
    "create.--new-host": "provider_names",
    "list.--provider": "provider_names",
}

# Maps field annotation types (from config models) to completion source names.
# When _walk_model_for_choices encounters a field with one of these types, it
# uses the corresponding source name to look up dynamic completion values.
_FIELD_TYPE_COMPLETION_SOURCES: Final[dict[type, str]] = {
    AgentTypeName: "agent_type_names",
    ProviderBackendName: "provider_backend_names",
}


# =============================================================================
# Cache writers
# =============================================================================


def _extract_options_for_command(cmd: click.Command) -> list[str]:
    """Extract all --long option names from a click command."""
    options: list[str] = []
    for param in cmd.params:
        if isinstance(param, click.Option):
            for opt in param.opts + param.secondary_opts:
                if opt.startswith("--"):
                    options.append(opt)
    return sorted(options)


def _extract_flag_options_for_command(cmd: click.Command) -> list[str]:
    """Extract all option names (both --long and -short) where ``is_flag`` is True."""
    flags: list[str] = []
    for param in cmd.params:
        if isinstance(param, click.Option) and param.is_flag:
            for opt in param.opts + param.secondary_opts:
                flags.append(opt)
    return sorted(flags)


def _extract_choices_for_command(cmd: click.Command, key_prefix: str) -> dict[str, list[str]]:
    """Extract option choices (click.Choice values) from a click command.

    Returns a dict mapping "key_prefix.--option" to the list of valid choices.
    """
    choices: dict[str, list[str]] = {}
    for param in cmd.params:
        if isinstance(param, click.Option) and isinstance(param.type, click.Choice):
            choice_values: list[str] = [str(c) for c in param.type.choices]
            for opt in param.opts + param.secondary_opts:
                if opt.startswith("--"):
                    choices[f"{key_prefix}.{opt}"] = choice_values
    return choices


def _filter_keys_by_registered_commands(
    dotted_keys: frozenset[str],
    canonical_names: set[str],
) -> set[str]:
    """Return the subset of dotted keys whose top-level command is in canonical_names.

    Works for both "command.--option" keys (e.g. "create.--host") and
    "group.subcommand" keys (e.g. "plugin.enable"). The first component
    before the dot is always the command/group name.
    """
    return {key for key in dotted_keys if key.split(".")[0] in canonical_names}


def _extract_positional_nargs(cmd: click.Command) -> int | None:
    """Extract the total positional argument count from a click command.

    Returns the sum of nargs for all click.Argument params, or None if any
    argument has nargs=-1 (unlimited). Returns 0 if there are no positional
    arguments.
    """
    total = 0
    for param in cmd.params:
        if isinstance(param, click.Argument):
            if param.nargs == -1:
                return None
            total += param.nargs
    return total


def _extract_plugin_name_options_for_command(cmd: click.Command, key_prefix: str) -> list[str]:
    """Extract option names that should complete against plugin names.

    Returns keys like "create.--plugin" for options matching _PLUGIN_NAME_OPTION_NAMES.
    """
    result: list[str] = []
    for param in cmd.params:
        if isinstance(param, click.Option):
            for opt in param.opts + param.secondary_opts:
                if opt in _PLUGIN_NAME_OPTION_NAMES:
                    result.append(f"{key_prefix}.{opt}")
    return result


def flatten_dict_keys(data: dict[str, Any], prefix: str = "") -> list[str]:
    """Flatten a nested dict into sorted dot-separated key paths."""
    result: list[str] = []
    for key, value in data.items():
        full_key = f"{prefix}{key}" if prefix else key
        if isinstance(value, dict):
            result.extend(flatten_dict_keys(value, f"{full_key}."))
        else:
            result.append(full_key)
    return sorted(result)


def _unwrap_optional(annotation: Any) -> Any:
    """Unwrap Optional[T] or T | None to get the inner type T.

    Python 3.10+ uses types.UnionType for X | Y syntax;
    typing.Optional[X] / typing.Union[X, None] uses typing.Union.
    Returns the annotation unchanged if it is not an Optional wrapper.
    """
    if isinstance(annotation, types.UnionType):
        args = [a for a in annotation.__args__ if a is not type(None)]
        if len(args) == 1:
            return args[0]
        return annotation
    if hasattr(annotation, "__origin__") and annotation.__origin__ is typing.Union:
        args = [a for a in annotation.__args__ if a is not type(None)]
        if len(args) == 1:
            return args[0]
        return annotation
    return annotation


def _extract_config_value_choices(
    config: BaseModel,
    dynamic_values: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Walk a config instance to find all fields with constrained-value types.

    For bool fields, returns ["true", "false"].
    For Enum subclass fields, returns the string values of the enum members.
    For fields whose annotation type is in _FIELD_TYPE_COMPLETION_SOURCES,
    returns the corresponding dynamic completion values.
    For nested BaseModel fields, recurses with a dotted prefix.
    For dict fields whose values are BaseModel instances, iterates the
    concrete keys from the instance and recurses into each value.
    Handles Optional[T] / T | None annotations by unwrapping to the inner type.
    """
    resolved = dynamic_values if dynamic_values is not None else {}
    result: dict[str, list[str]] = {}
    _walk_model_for_choices(config, "", result, resolved)
    return result


def _walk_model_for_choices(
    obj: BaseModel,
    prefix: str,
    result: dict[str, list[str]],
    dynamic_values: dict[str, list[str]],
) -> None:
    """Recursively walk a pydantic model instance, collecting constrained-value fields."""
    field_values = obj.__dict__
    for field_name, field_info in type(obj).model_fields.items():
        key = f"{prefix}{field_name}" if prefix else field_name
        value = field_values[field_name]
        annotation = _unwrap_optional(field_info.annotation)

        if annotation is bool:
            result[key] = ["true", "false"]
        elif isinstance(annotation, type) and issubclass(annotation, Enum):
            result[key] = [str(e.value) for e in annotation]
        elif annotation in _FIELD_TYPE_COMPLETION_SOURCES:
            source_name = _FIELD_TYPE_COMPLETION_SOURCES[annotation]
            values = dynamic_values.get(source_name, [])
            if values:
                result[key] = values
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            _walk_model_for_choices(value, f"{key}.", result, dynamic_values)
        elif isinstance(value, dict):
            for dict_key, dict_value in value.items():
                if isinstance(dict_value, BaseModel):
                    _walk_model_for_choices(dict_value, f"{key}.{dict_key}.", result, dynamic_values)
        else:
            # Other types (str, int, Path, list, etc.) have no constrained
            # value set -- skip them.
            continue


class _DynamicCompletions(NamedTuple):
    """Dynamic completion data extracted from the runtime context."""

    agent_type_names: list[str]
    template_names: list[str]
    provider_names: list[str]
    plugin_names: list[str]
    config_keys: list[str]
    config_value_choices: dict[str, list[str]]


def _is_excluded_config_key(key: str) -> bool:
    """Return True if *key* matches any excluded config key prefix."""
    return any(key == prefix or key.startswith(f"{prefix}.") for prefix in _EXCLUDED_CONFIG_KEY_PREFIXES)


def _build_dynamic_completions(
    mngr_ctx: MngrContext,
    registered_agent_types: list[str],
) -> _DynamicCompletions:
    """Build dynamic completion data from the runtime context.

    Extracts agent type names, template names, provider names, plugin names,
    and config keys from the live MngrContext for injection into the cache.
    """
    config = mngr_ctx.config

    custom = [str(k) for k in config.agent_types.keys()]
    agent_type_names = sorted(set(registered_agent_types + custom))

    provider_backend_names = list_registered_provider_backend_names()

    template_names = sorted(str(k) for k in config.create_templates.keys())
    provider_names = sorted(set(["local"] + [str(k) for k in config.providers.keys()]))
    plugin_names = sorted({name for name, _ in mngr_ctx.pm.list_name_plugin() if name and not name.startswith("_")})
    config_keys = [k for k in flatten_dict_keys(config.model_dump(mode="json")) if not _is_excluded_config_key(k)]

    dynamic_values = {
        "agent_type_names": agent_type_names,
        "provider_backend_names": provider_backend_names,
    }
    config_value_choices = {
        k: v
        for k, v in _extract_config_value_choices(config, dynamic_values).items()
        if not _is_excluded_config_key(k)
    }

    return _DynamicCompletions(
        agent_type_names=agent_type_names,
        template_names=template_names,
        provider_names=provider_names,
        plugin_names=plugin_names,
        config_keys=config_keys,
        config_value_choices=config_value_choices,
    )


def write_cli_completions_cache(
    *,
    cli_group: click.Group,
    mngr_ctx: MngrContext | None = None,
    registered_agent_types: list[str] | None = None,
) -> None:
    """Write all CLI commands, options, and choices to the completions cache (best-effort).

    Walks the CLI command tree and writes the result to
    .command_completions.json in the completion cache directory. This is called
    from the list command (triggered by background tab completion refresh) to
    keep the cache up to date with installed plugins.

    Aliases are auto-detected: any command registered under a name different
    from its canonical cmd.name is treated as an alias.

    When mngr_ctx is provided, runtime-derived completion values (agent types,
    templates, providers, plugin names, config keys) are extracted and injected
    into the cache.

    Catches OSError from cache writes so filesystem failures do not break
    CLI commands. Other exceptions are allowed to propagate.
    """
    try:
        all_command_names = sorted(cli_group.commands.keys())
        alias_to_canonical = detect_alias_to_canonical(cli_group)

        subcommand_by_command: dict[str, list[str]] = {}
        options_by_command: dict[str, list[str]] = {}
        flag_options_by_command: dict[str, list[str]] = {}
        option_choices: dict[str, list[str]] = {}
        plugin_name_opts: list[str] = []
        positional_nargs_by_command: dict[str, int | None] = {}

        canonical_names: set[str] = set()
        for name, cmd in cli_group.commands.items():
            # Skip alias entries -- only process canonical command names
            if name in alias_to_canonical:
                continue

            canonical_name = cmd.name or name
            canonical_names.add(canonical_name)

            if isinstance(cmd, click.Group) and cmd.commands:
                if canonical_name not in subcommand_by_command:
                    subcommand_by_command[canonical_name] = sorted(cmd.commands.keys())

                # Extract options, flags, choices, and positional nargs for subcommands
                for sub_name, sub_cmd in cmd.commands.items():
                    sub_key = f"{canonical_name}.{sub_name}"
                    sub_options = _extract_options_for_command(sub_cmd)
                    if sub_options:
                        options_by_command[sub_key] = sub_options
                    sub_flags = _extract_flag_options_for_command(sub_cmd)
                    if sub_flags:
                        flag_options_by_command[sub_key] = sub_flags
                    option_choices.update(_extract_choices_for_command(sub_cmd, sub_key))
                    plugin_name_opts.extend(_extract_plugin_name_options_for_command(sub_cmd, sub_key))
                    positional_nargs_by_command[sub_key] = _extract_positional_nargs(sub_cmd)

                # Also extract options and flags for the group command itself
                group_options = _extract_options_for_command(cmd)
                if group_options:
                    options_by_command[canonical_name] = group_options
                group_flags = _extract_flag_options_for_command(cmd)
                if group_flags:
                    flag_options_by_command[canonical_name] = group_flags
                option_choices.update(_extract_choices_for_command(cmd, canonical_name))
                plugin_name_opts.extend(_extract_plugin_name_options_for_command(cmd, canonical_name))
            else:
                # Simple command (not a group)
                cmd_options = _extract_options_for_command(cmd)
                if cmd_options:
                    options_by_command[canonical_name] = cmd_options
                cmd_flags = _extract_flag_options_for_command(cmd)
                if cmd_flags:
                    flag_options_by_command[canonical_name] = cmd_flags
                option_choices.update(_extract_choices_for_command(cmd, canonical_name))
                plugin_name_opts.extend(_extract_plugin_name_options_for_command(cmd, canonical_name))
                positional_nargs_by_command[canonical_name] = _extract_positional_nargs(cmd)

        git_branch_opts = _filter_keys_by_registered_commands(_GIT_BRANCH_OPTIONS, canonical_names)
        host_name_opts = _filter_keys_by_registered_commands(_HOST_NAME_OPTIONS, canonical_names)

        # Build per-position positional completions from the spec dicts,
        # filtering to only include commands that are actually registered.
        positional_completions: dict[str, list[list[str]]] = {}
        for cmd_name, entries in _POSITIONAL_COMPLETION_SPEC.items():
            if cmd_name in canonical_names:
                positional_completions[cmd_name] = entries
        for dotted_key, entries in _POSITIONAL_COMPLETION_SUBCOMMAND_SPEC.items():
            if dotted_key.split(".")[0] in canonical_names:
                positional_completions[dotted_key] = entries

        # Inject dynamic choice values from runtime context (config, registries)
        dynamic = _build_dynamic_completions(mngr_ctx, registered_agent_types or []) if mngr_ctx is not None else None
        if dynamic is not None:
            dynamic_as_dict = dynamic._asdict()
            for opt_key, data_key in _DYNAMIC_CHOICE_OPTIONS.items():
                cmd_name = opt_key.split(".")[0]
                if cmd_name in canonical_names and data_key in dynamic_as_dict:
                    option_choices[opt_key] = dynamic_as_dict[data_key]

        cache_data = CompletionCacheData(
            commands=all_command_names,
            aliases=alias_to_canonical,
            subcommand_by_command=subcommand_by_command,
            options_by_command=options_by_command,
            flag_options_by_command=flag_options_by_command,
            option_choices=option_choices,
            git_branch_options=sorted(git_branch_opts),
            host_name_options=sorted(host_name_opts),
            plugin_name_options=sorted(set(plugin_name_opts)),
            plugin_names=dynamic.plugin_names if dynamic is not None else [],
            config_keys=dynamic.config_keys if dynamic is not None else [],
            positional_nargs_by_command=positional_nargs_by_command,
            positional_completions=positional_completions,
            config_value_choices=dynamic.config_value_choices if dynamic is not None else {},
        )

        cache_path = get_completion_cache_dir() / COMPLETION_CACHE_FILENAME
        atomic_write(cache_path, json.dumps(cache_data._asdict()))
    except OSError:
        logger.debug("Failed to write CLI completions cache")
