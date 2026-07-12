import json
from collections.abc import Iterator
from enum import Enum
from typing import Any
from typing import Final
from typing import NamedTuple

import click
from loguru import logger
from pydantic import BaseModel

from imbue.mngr.config.agent_config_registry import get_agent_config_class
from imbue.mngr.config.completion_cache import COMPLETION_CACHE_FILENAME
from imbue.mngr.config.completion_cache import CompletionCacheData
from imbue.mngr.config.completion_cache import get_completion_cache_dir
from imbue.mngr.config.data_types import CreateCliOptions
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.config.provider_config_registry import list_registered_provider_backend_names
from imbue.mngr.plugin_catalog import get_installable_packages
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.utils.click_utils import detect_alias_to_canonical
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr.utils.model_schema import walk_model_fields
from imbue.mngr.utils.pydantic_utils import unwrap_optional

# Per-position positional completion spec for top-level commands.
# Maps command name -> list of source identifier lists per position.
# Each inner list contains source names for that position (empty = freeform).
# For variadic commands (nargs=None), the last entry repeats.
# Source identifiers: "agent_names", "host_names", "plugin_names",
# "catalog_packages", "installed_packages", "config_keys", "help_targets"
_POSITIONAL_COMPLETION_SPEC: Final[dict[str, list[list[str]]]] = {
    "archive": [["agent_names"]],
    "capture": [["agent_names"]],
    "connect": [["agent_names"]],
    "destroy": [["agent_names"]],
    "exec": [["agent_names"]],
    "limit": [["agent_names"]],
    "event": [["agent_names", "host_names"], []],
    "help": [["help_targets"]],
    "label": [["agent_names"]],
    "message": [["agent_names"]],
    "pair": [["agent_names"]],
    "pull": [["agent_names"], []],
    "push": [["agent_names"], []],
    "rename": [["agent_names"], []],
    "start": [["agent_names"]],
    "stop": [["agent_names"]],
    "transcript": [["agent_names", "host_names"]],
}

# Per-position positional completion spec for group subcommands.
# Uses dotted notation: "group.subcommand".
_POSITIONAL_COMPLETION_SUBCOMMAND_SPEC: Final[dict[str, list[list[str]]]] = {
    "snapshot.create": [["agent_names"]],
    "snapshot.destroy": [["agent_names"]],
    "snapshot.list": [["agent_names"]],
    "plugin.add": [["catalog_packages"]],
    "plugin.remove": [["installed_packages"]],
    "plugin.enable": [["plugin_names"]],
    "plugin.disable": [["plugin_names"]],
    "config.get": [["config_keys"]],
    "config.set": [["config_keys"], ["config_value_for_key"]],
    "config.unset": [["config_keys"]],
    "file.get": [["agent_names", "host_names"], []],
    "file.put": [["agent_names", "host_names"], []],
    "file.list": [["agent_names", "host_names"], []],
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
_HOST_NAME_OPTIONS: Final[frozenset[str]] = frozenset()

# Click option names (--long forms) that should complete against plugin names.
_PLUGIN_NAME_OPTION_NAMES: Final[frozenset[str]] = frozenset(
    {
        "--plugin",
        "--enable-plugin",
        "--disable-plugin",
    }
)

# Option names whose value is a ``KEY=VALUE`` config override (the ``-S``/
# ``--setting`` common option). The completer completes their KEY against
# config_keys and their VALUE against config_value_choices. These are global
# common options (see ``add_common_options``), so they are recorded once by
# name rather than per command.
_SETTING_OPTION_NAMES: Final[frozenset[str]] = frozenset(
    {
        "-S",
        "--setting",
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
    """Extract every option name from a click command, both ``--long`` and ``-short`` forms.

    This is the full set of recognised options. It serves two roles in the
    completer: the ``--long`` entries are the candidates offered when completing
    ``--`` (short forms are filtered out there by the ``--`` prefix), and the
    whole set lets the positional-argument counter (``_count_positional_words``)
    recognise an option so it knows to consume its value. A value-taking option
    consumes the following word, so a short option like ``-S KEY=VALUE`` must be
    recognised here to avoid miscounting its value as a positional argument.
    No-value options (flags and ``count`` options) are additionally recorded in
    flag_options so the counter consumes only the option word itself.
    """
    options: list[str] = []
    for param in cmd.params:
        if isinstance(param, click.Option):
            options.extend(param.opts + param.secondary_opts)
    return sorted(options)


def _extract_flag_options_for_command(cmd: click.Command) -> list[str]:
    """Extract no-value option names (both --long and -short forms).

    These are the options that take no value: boolean flags (``is_flag``) and
    repeatable counters (``count``, e.g. ``-v``/``--verbose``). The
    positional-argument counter consumes only the option word itself for these,
    rather than also consuming the following word as it does for value-taking
    options. Both the long and short forms are recorded so they are treated
    uniformly.
    """
    flags: list[str] = []
    for param in cmd.params:
        if isinstance(param, click.Option) and (param.is_flag or param.count):
            flags.extend(param.opts + param.secondary_opts)
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


def _value_choices_for_annotation(
    annotation: Any,
    dynamic_values: dict[str, list[str]],
) -> list[str] | None:
    """Return the constrained value set for a field annotation, or None if unconstrained.

    ``bool`` -> ``["true", "false"]``; an ``Enum`` subclass -> its member values; a
    type in ``_FIELD_TYPE_COMPLETION_SOURCES`` -> the corresponding dynamic values
    (or None when none are available). ``Optional[T]`` / ``T | None`` is unwrapped
    first. Everything else (str, int, Path, list, dict, ...) is unconstrained.
    """
    annotation = unwrap_optional(annotation)
    if annotation is bool:
        return ["true", "false"]
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return [str(e.value) for e in annotation]
    if annotation in _FIELD_TYPE_COMPLETION_SOURCES:
        return dynamic_values.get(_FIELD_TYPE_COMPLETION_SOURCES[annotation]) or None
    return None


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
        annotation = unwrap_optional(field_info.annotation)

        choices = _value_choices_for_annotation(annotation, dynamic_values)
        if choices is not None:
            result[key] = choices
        elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
            _walk_model_for_choices(value, f"{key}.", result, dynamic_values)
        elif isinstance(value, dict):
            for dict_key, dict_value in value.items():
                if isinstance(dict_value, BaseModel):
                    _walk_model_for_choices(dict_value, f"{key}.{dict_key}.", result, dynamic_values)
        else:
            # Other types (str, int, Path, list, etc.) have no constrained value set -- skip them.
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


# Dict-container config namespaces whose completion keys are built from the
# config *schema* (and enumerable key sources) rather than by flattening the
# config instance. Their instance-dump keys use the internal
# ``.defaults.``/``.options.`` shape and drop unset fields, so they are excluded
# from the flattened base key set and re-emitted correctly per namespace.
_CONTAINER_NAMESPACES: Final[frozenset[str]] = frozenset(
    {"agent_types", "providers", "plugins", "commands", "create_templates", "pre_command_scripts"}
)


def _is_container_namespace_key(key: str) -> bool:
    """Return True for any key under a schema-completed dict-container namespace."""
    return key.split(".", 1)[0] in _CONTAINER_NAMESPACES


def _collect_model_field_completions(
    config_class: type[BaseModel],
    prefix: tuple[str, ...],
    dynamic_values: dict[str, list[str]],
) -> tuple[list[str], dict[str, list[str]]]:
    """Walk one config model's schema into completion keys and constrained-value choices.

    Returns ``(keys, choices)``: one key per settable leaf field (dotted, under
    ``prefix``), plus a value-choices entry for each field whose annotation
    constrains its value (bool, enum, or a dynamic-source type). Shared by the
    ``agent_types`` / ``plugins`` / ``providers`` namespace builders, which differ
    only in how they resolve each entry's config class.
    """
    keys: list[str] = []
    choices: dict[str, list[str]] = {}
    for path, annotation, _description in walk_model_fields(config_class, prefix=prefix, recurse_optional=True):
        keys.append(path)
        value_choices = _value_choices_for_annotation(annotation, dynamic_values)
        if value_choices is not None:
            choices[path] = value_choices
    return keys, choices


def _agent_type_schema_completions(
    agent_type_names: list[str],
    config: MngrConfig,
    dynamic_values: dict[str, list[str]],
) -> tuple[list[str], dict[str, list[str]]]:
    """Build ``agent_types.<name>.*`` completion keys and value choices from each type's config schema.

    Returns ``(keys, value_choices)``. Unlike dumping the config instance (which
    only covers agent types defined in the user's config, and drops unset
    container fields), this walks the *schema* of every known agent type's config
    class -- builtin/registered types as well as custom ones -- so e.g.
    ``agent_types.claude.config_overrides`` is offered even when nothing is set.

    For a custom type the resolved instance's class is used (it carries the parent
    type's subclass fields); for a builtin type the registered config class is
    used (falling back to the base ``AgentTypeConfig``).
    """
    keys: list[str] = []
    choices: dict[str, list[str]] = {}
    for name in agent_type_names:
        existing = config.agent_types.get(AgentTypeName(name))
        config_class = type(existing) if existing is not None else get_agent_config_class(name)
        sub_keys, sub_choices = _collect_model_field_completions(config_class, ("agent_types", name), dynamic_values)
        keys.extend(sub_keys)
        choices.update(sub_choices)
    return keys, choices


def _plugin_schema_completions(
    plugin_names: list[str],
    config: MngrConfig,
    dynamic_values: dict[str, list[str]],
) -> tuple[list[str], dict[str, list[str]]]:
    """Build ``plugins.<name>.*`` keys/choices from each plugin's config schema.

    Plugin names are enumerable (installed plugins), so each plugin's config
    fields are offered even before anything is set for it. A configured plugin
    with a plugin-specific ``PluginConfig`` subclass uses that subclass's schema;
    otherwise the base ``PluginConfig`` (``enabled``) is walked.
    """
    keys: list[str] = []
    choices: dict[str, list[str]] = {}
    names = sorted(set(plugin_names) | {str(k) for k in config.plugins.keys()})
    for name in names:
        existing = config.plugins.get(PluginName(name))
        config_class = type(existing) if existing is not None else PluginConfig
        sub_keys, sub_choices = _collect_model_field_completions(config_class, ("plugins", name), dynamic_values)
        keys.extend(sub_keys)
        choices.update(sub_choices)
    return keys, choices


def _provider_schema_completions(
    config: MngrConfig,
    dynamic_values: dict[str, list[str]],
) -> tuple[list[str], dict[str, list[str]]]:
    """Build ``providers.<name>.*`` keys/choices from each configured provider's schema.

    Provider instance names are user-chosen (not enumerable), so only configured
    providers contribute -- but each is walked from its config *schema*, so all
    settable fields (e.g. the discovery timeouts) are offered even when unset,
    not just the fields the user already wrote (which is all the instance dump
    would surface).
    """
    keys: list[str] = []
    choices: dict[str, list[str]] = {}
    for name, instance in config.providers.items():
        sub_keys, sub_choices = _collect_model_field_completions(
            type(instance), ("providers", str(name)), dynamic_values
        )
        keys.extend(sub_keys)
        choices.update(sub_choices)
    return keys, choices


def _create_template_schema_completions(
    config: MngrConfig,
    dynamic_values: dict[str, list[str]],
    create_param_choices: dict[str, list[str]],
) -> tuple[list[str], dict[str, list[str]]]:
    """Build ``create_templates.<name>.*`` keys/choices for configured templates.

    Template names are user-chosen (not enumerable), so only configured templates
    contribute. A template's options are validated against ``CreateCliOptions``
    (see ``loader._parse_create_templates``), so every ``CreateCliOptions`` field
    is offered as a settable ``create_templates.<name>.<param>`` -- the
    transparently unwrapped, user-facing key -- rather than only the params
    already present (and rather than the internal ``.options.<param>`` shape the
    instance dump would produce, which does not round-trip through ``config set``).

    Value choices come from ``create_param_choices`` -- the ``create`` command's own
    option choices, static and dynamic -- so a template option completes to the same
    values ``mngr create --<opt>`` does (e.g. ``type`` -> agent type names). Fields
    with no create-option choice fall back to what the annotation alone constrains.
    """
    keys: list[str] = []
    choices: dict[str, list[str]] = {}
    for name in config.create_templates.keys():
        for field_name, field_info in CreateCliOptions.model_fields.items():
            key = f"create_templates.{name}.{field_name}"
            keys.append(key)
            value_choices = create_param_choices.get(field_name)
            if value_choices is None:
                value_choices = _value_choices_for_annotation(field_info.annotation, dynamic_values)
            if value_choices is not None:
                choices[key] = value_choices
    return keys, choices


def _option_value_choices(option: click.Option) -> list[str] | None:
    """Return the constrained value set for a click option, or None if freeform.

    A ``click.Choice`` yields its choices; a boolean flag yields
    ``["true", "false"]``. Everything else has no fixed value set.
    """
    if isinstance(option.type, click.Choice):
        return [str(choice) for choice in option.type.choices]
    if option.is_flag:
        return ["true", "false"]
    return None


def _create_command_param_choices(
    create_cmd: click.Command | None,
    dynamic_choice_values: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Map each ``create`` option's param name to its value choices (static + dynamic).

    A create template's options are exactly the ``create`` command's options (a
    template is validated against ``CreateCliOptions``), so a template option should
    complete to the same values ``mngr create --<opt>`` does. Static choices come
    from the option itself (booleans, ``click.Choice``); dynamic choices come from
    ``_DYNAMIC_CHOICE_OPTIONS`` (e.g. ``--type`` -> agent type names, ``--provider``
    -> provider names), looked up in ``dynamic_choice_values``. Returns an empty map
    when the ``create`` command is absent (e.g. disabled).
    """
    if create_cmd is None:
        return {}
    choices: dict[str, list[str]] = {}
    for param in create_cmd.params:
        if not isinstance(param, click.Option) or not param.name:
            continue
        static_choices = _option_value_choices(param)
        if static_choices is not None:
            choices[param.name] = static_choices
            continue
        for opt in param.opts:
            data_key = _DYNAMIC_CHOICE_OPTIONS.get(f"create.{opt}")
            if data_key is not None and data_key in dynamic_choice_values:
                choices[param.name] = dynamic_choice_values[data_key]
                break
    return choices


def _iter_leaf_commands(cli_group: click.Group) -> Iterator[tuple[str | None, str, click.Command]]:
    """Yield ``(group_name, leaf_name, command)`` for every non-group command in the tree.

    ``group_name`` is None for a top-level command and the group's canonical name
    for a subcommand. Alias entries (registered under a name other than the
    command's canonical name) are skipped so each command is yielded once, under
    its canonical name.
    """
    top_aliases = detect_alias_to_canonical(cli_group)
    for name, cmd in cli_group.commands.items():
        if name in top_aliases:
            continue
        if isinstance(cmd, click.Group) and cmd.commands:
            group_name = cmd.name or name
            sub_aliases = detect_alias_to_canonical(cmd)
            for sub_name, sub_cmd in cmd.commands.items():
                if sub_name in sub_aliases:
                    continue
                yield group_name, (sub_cmd.name or sub_name), sub_cmd
        else:
            yield None, (cmd.name or name), cmd


def _derive_command_name(group_name: str | None, leaf_name: str) -> str:
    """Derive a command's ``command_name`` bucket from its position in the tree.

    Top-level commands use their own name; group subcommands are namespaced
    ``<group>_<subcommand>`` (matching the call-site literals -- see
    ``cli/command_names.py``). Callers gate the result on the authoritative
    registry, so a derived name that does not correspond to a real bucket (e.g.
    ``config_set`` for the group-level ``config`` bucket) is discarded.
    """
    return leaf_name if group_name is None else f"{group_name}_{leaf_name}"


def _command_defaults_completions(
    cli_group: click.Group,
    config: MngrConfig,
    config_command_names: frozenset[str],
    default_subcommand_choices: dict[str, list[str]],
    dynamic_values: dict[str, list[str]],
) -> tuple[list[str], dict[str, list[str]]]:
    """Build ``commands.*`` completion keys/choices in the user-facing key shape.

    Users set ``commands.<name>.<param>`` (the ``defaults`` wrapper is transparent
    -- see ``config/key_resolver.py``), so keys are emitted flat, never in the
    internal ``commands.<name>.defaults.<param>`` shape (which does not round-trip
    through ``config set``). Three sources:

    - Per-command parameter defaults: for every tree command whose derived
      ``command_name`` (``<group>_<sub>`` for subcommands) owns a defaults bucket
      (is in ``config_command_names``), each click option is offered as
      ``commands.<command_name>.<param>`` -- discoverable before anything is set.
      Boolean/choice options also complete their value. Gating on the registry
      excludes the group-level ``config`` / ``plugin`` buckets, whose derived
      per-subcommand names are absent from it.
    - ``default_subcommand``: for every configurable-default group,
      ``commands.<config_key>.default_subcommand`` with the group's subcommand
      names as value choices. The (config_key -> subcommand names) mapping is
      computed in the cli layer and passed in as ``default_subcommand_choices``.
    - Already-configured params: any ``commands.<name>.<param>`` present in the
      user's config, covering plugin-added options and the group-level buckets
      that have no per-command tree entry.
    """
    keys: list[str] = []
    choices: dict[str, list[str]] = {}

    for group_name, leaf_name, cmd in _iter_leaf_commands(cli_group):
        command_name = _derive_command_name(group_name, leaf_name)
        if command_name not in config_command_names:
            continue
        for param in cmd.params:
            if isinstance(param, click.Option) and param.expose_value and param.name:
                key = f"commands.{command_name}.{param.name}"
                keys.append(key)
                value_choices = _option_value_choices(param)
                if value_choices is not None:
                    choices[key] = value_choices

    for config_key, subcommand_names in default_subcommand_choices.items():
        key = f"commands.{config_key}.default_subcommand"
        keys.append(key)
        if subcommand_names:
            choices[key] = subcommand_names

    for name, command_defaults in config.commands.items():
        for param_name in command_defaults.defaults.keys():
            keys.append(f"commands.{name}.{param_name}")

    return keys, choices


def _build_dynamic_completions(
    mngr_ctx: MngrContext,
    registered_agent_types: list[str],
    cli_group: click.Group,
    config_command_names: list[str],
    default_subcommand_choices: dict[str, list[str]],
) -> _DynamicCompletions:
    """Build dynamic completion data from the runtime context.

    Extracts agent type names, template names, provider names, plugin names,
    and config keys from the live MngrContext for injection into the cache.

    The dict-container config namespaces (``agent_types``, ``providers``,
    ``plugins``, ``commands``, ``create_templates``, ``pre_command_scripts``) are
    completed from the config *schema* and enumerable key sources (registered
    agent/plugin names, the ``command_name`` registry, the command tree) rather
    than from flattening the config instance, so every settable key -- not just
    the already-configured ones -- is offered, in the correct user-facing shape.
    """
    config = mngr_ctx.config

    custom = [str(k) for k in config.agent_types.keys()]
    agent_type_names = sorted(set(registered_agent_types + custom))

    provider_backend_names = list_registered_provider_backend_names()

    template_names = sorted(str(k) for k in config.create_templates.keys())
    provider_names = sorted(set(["local"] + [str(k) for k in config.providers.keys()]))
    plugin_names = sorted({name for name, _ in mngr_ctx.pm.list_name_plugin() if name and not name.startswith("_")})

    dynamic_values = {
        "agent_type_names": agent_type_names,
        "provider_backend_names": provider_backend_names,
    }

    command_name_set = frozenset(config_command_names)

    # Each dict-container namespace's keys/choices come from the config schema
    # (and its enumerable key source), not the instance dump -- so unset fields
    # and, where the key source is authoritative (agent/plugin names, the command
    # registry), unconfigured entries are still offered.
    schema_agent_keys, schema_agent_choices = _agent_type_schema_completions(agent_type_names, config, dynamic_values)
    plugin_keys, plugin_choices = _plugin_schema_completions(plugin_names, config, dynamic_values)
    provider_keys, provider_choices = _provider_schema_completions(config, dynamic_values)
    command_keys, command_choices = _command_defaults_completions(
        cli_group, config, command_name_set, default_subcommand_choices, dynamic_values
    )
    # A create template's option values complete like ``mngr create --<opt>``, so
    # reuse the create command's own choices (incl. the dynamic ``--type`` etc.).
    create_param_choices = _create_command_param_choices(
        cli_group.commands.get("create"),
        {"agent_type_names": agent_type_names, "template_names": template_names, "provider_names": provider_names},
    )
    template_keys, template_choices = _create_template_schema_completions(
        config, dynamic_values, create_param_choices
    )
    pre_command_script_keys = [
        f"pre_command_scripts.{name}"
        for name in sorted(command_name_set | {str(k) for k in config.pre_command_scripts.keys()})
    ]

    # Non-container keys come from the instance dump unchanged; container keys are
    # dropped here and replaced by the schema-derived keys above.
    base_keys = [k for k in flatten_dict_keys(config.model_dump(mode="json")) if not _is_container_namespace_key(k)]
    all_keys = (
        base_keys
        + schema_agent_keys
        + plugin_keys
        + provider_keys
        + command_keys
        + template_keys
        + pre_command_script_keys
    )
    config_keys = sorted({k for k in all_keys if not _is_excluded_config_key(k)})

    base_choices = {
        k: v
        for k, v in _extract_config_value_choices(config, dynamic_values).items()
        if not _is_container_namespace_key(k)
    }
    merged_choices = {
        **base_choices,
        **schema_agent_choices,
        **plugin_choices,
        **provider_choices,
        **command_choices,
        **template_choices,
    }
    config_value_choices = {k: v for k, v in merged_choices.items() if not _is_excluded_config_key(k)}

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
    topic_names: list[str] | None = None,
    installed_plugin_packages: list[str] | None = None,
    config_command_names: list[str] | None = None,
    default_subcommand_choices: dict[str, list[str]] | None = None,
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

    topic_names are the registered ``mngr help`` topic keys; combined with the
    command names they form the completion candidates for the ``mngr help``
    positional argument. The caller passes these because help topics live in the
    cli layer, which this (config-layer) writer must not import.

    installed_plugin_packages are the package names currently installed as
    plugins (uv-tool receipt extras); they are the completion candidates for the
    ``mngr plugin remove`` positional argument. The caller passes these for the
    same layering reason as topic_names: the uv-tool receipt helper transitively
    imports the cli layer, which this writer must not depend on.

    config_command_names is the authoritative set of ``command_name`` values
    (``cli.command_names.KNOWN_CONFIG_COMMAND_NAMES``): the keys under which a
    command owns its ``[commands.<name>]`` parameter defaults and
    ``[pre_command_scripts.<name>]`` hooks. The caller passes it for the same
    layering reason as topic_names (the registry lives in the cli layer); the
    completion of ``commands.*`` / ``pre_command_scripts.*`` keys relies on it to
    recognise which tree commands own a defaults bucket.

    default_subcommand_choices maps each configurable-default group's config key
    (see ``cli.command_names.build_default_subcommand_choices``) to its subcommand
    names, so ``commands.<config_key>.default_subcommand`` completes with the right
    values. The caller computes it because it depends on the cli-layer
    ``DefaultCommandGroup`` type, which this writer must not import.

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

        # Candidates for `mngr help <arg>`: every top-level command plus every
        # registered help topic key. Only meaningful if the help command exists.
        help_targets: list[str] = []
        if "help" in canonical_names:
            help_targets = sorted(canonical_names | set(topic_names or []))

        # Inject dynamic choice values from runtime context (config, registries)
        dynamic = (
            _build_dynamic_completions(
                mngr_ctx,
                registered_agent_types or [],
                cli_group,
                config_command_names or [],
                default_subcommand_choices or {},
            )
            if mngr_ctx is not None
            else None
        )
        if dynamic is not None:
            dynamic_as_dict = dynamic._asdict()
            for opt_key, data_key in _DYNAMIC_CHOICE_OPTIONS.items():
                cmd_name = opt_key.split(".")[0]
                if cmd_name in canonical_names and data_key in dynamic_as_dict:
                    option_choices[opt_key] = dynamic_as_dict[data_key]

        # Static catalog package names for `mngr plugin add` completion. Sourced
        # from the plugin catalog (the same store the `mngr extras` install wizard
        # uses), not from the runtime context, so it is always available.
        catalog_package_names = sorted({entry.package_name for entry in get_installable_packages()})

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
            catalog_package_names=catalog_package_names,
            installed_plugin_package_names=sorted(set(installed_plugin_packages or [])),
            config_keys=dynamic.config_keys if dynamic is not None else [],
            positional_nargs_by_command=positional_nargs_by_command,
            positional_completions=positional_completions,
            config_value_choices=dynamic.config_value_choices if dynamic is not None else {},
            help_targets=help_targets,
            setting_option_names=sorted(_SETTING_OPTION_NAMES),
        )

        cache_path = get_completion_cache_dir() / COMPLETION_CACHE_FILENAME
        atomic_write(cache_path, json.dumps(cache_data._asdict()))
    except OSError as e:
        logger.warning("Failed to write CLI completions cache: {}", e)
