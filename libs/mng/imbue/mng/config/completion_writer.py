import json
from typing import Any
from typing import Final

import click
from loguru import logger

from imbue.mng.agents.agent_registry import list_registered_agent_types
from imbue.mng.config.completion_types import COMPLETION_CACHE_FILENAME
from imbue.mng.config.completion_types import CompletionCacheData
from imbue.mng.config.completion_types import get_completion_cache_dir
from imbue.mng.config.data_types import MngContext
from imbue.mng.utils.click_utils import detect_alias_to_canonical
from imbue.mng.utils.file_utils import atomic_write

# Commands whose positional arguments should complete against agent names.
# This list is used by the cache writer to populate agent_name_arguments
# in the completions cache. The lightweight completer (complete.py) reads
# this field to decide when to offer agent name completions.
_AGENT_NAME_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "connect",
        "destroy",
        "exec",
        "limit",
        "events",
        "message",
        "pair",
        "provision",
        "pull",
        "push",
        "rename",
        "start",
        "stop",
    }
)

# Subcommands (within groups) whose positional arguments should complete
# against agent names. Uses dotted notation: "group.subcommand".
_AGENT_NAME_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {
        "snapshot.create",
        "snapshot.destroy",
        "snapshot.list",
    }
)

# Options (keyed as "command.--option") whose values should complete against
# git branch names. The lightweight completer reads this field to decide when
# to offer git branch completions.
_GIT_BRANCH_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "create.--base-branch",
    }
)

# Options whose values should complete against host names from the discovery
# event stream. Uses the same "command.--option" notation.
_HOST_NAME_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "create.--host",
        "create.--target-host",
    }
)

# Commands whose positional arguments should also complete against host names
# (in addition to agent names, if they appear in _AGENT_NAME_COMMANDS).
_HOST_NAME_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "events",
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

# Subcommands whose positional arguments should complete against plugin names.
_PLUGIN_NAME_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {
        "plugin.enable",
        "plugin.disable",
    }
)

# Subcommands whose positional arguments should complete against config keys.
_CONFIG_KEY_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {
        "config.get",
        "config.set",
        "config.unset",
    }
)

# Options that receive dynamic choice values from runtime context (config,
# registries). Maps "command.--option" to the key in dynamic_completions.
_DYNAMIC_CHOICE_OPTIONS: Final[dict[str, str]] = {
    "create.--agent-type": "agent_type_names",
    "create.--template": "template_names",
    "create.--in": "provider_names",
    "create.--new-host": "provider_names",
    "list.--provider": "provider_names",
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


def _build_dynamic_completions(mng_ctx: MngContext) -> dict[str, list[str]]:
    """Build dynamic completion data from the runtime context.

    Extracts agent type names, template names, provider names, plugin names,
    and config keys from the live MngContext for injection into the cache.
    """
    config = mng_ctx.config

    registered = list_registered_agent_types()
    custom = [str(k) for k in config.agent_types.keys()]
    agent_type_names = sorted(set(registered + custom))

    template_names = sorted(str(k) for k in config.create_templates.keys())
    provider_names = sorted(set(["local"] + [str(k) for k in config.providers.keys()]))
    plugin_names = sorted({name for name, _ in mng_ctx.pm.list_name_plugin() if name and not name.startswith("_")})
    config_keys = flatten_dict_keys(config.model_dump(mode="json"))

    return {
        "agent_type_names": agent_type_names,
        "template_names": template_names,
        "provider_names": provider_names,
        "plugin_names": plugin_names,
        "config_keys": config_keys,
    }


def write_cli_completions_cache(
    *,
    cli_group: click.Group,
    mng_ctx: MngContext | None = None,
) -> None:
    """Write all CLI commands, options, and choices to the completions cache (best-effort).

    Walks the CLI command tree and writes the result to
    .command_completions.json in the completion cache directory. This is called
    from the list command (triggered by background tab completion refresh) to
    keep the cache up to date with installed plugins.

    Aliases are auto-detected: any command registered under a name different
    from its canonical cmd.name is treated as an alias.

    When mng_ctx is provided, runtime-derived completion values (agent types,
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

                # Extract options, flags, and choices for subcommands
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

        # Include both top-level commands and group subcommands that take agent names
        agent_name_args = (_AGENT_NAME_COMMANDS & canonical_names) | _filter_keys_by_registered_commands(
            _AGENT_NAME_SUBCOMMANDS, canonical_names
        )

        git_branch_opts = _filter_keys_by_registered_commands(_GIT_BRANCH_OPTIONS, canonical_names)
        host_name_opts = _filter_keys_by_registered_commands(_HOST_NAME_OPTIONS, canonical_names)
        host_name_args = _HOST_NAME_COMMANDS & canonical_names
        plugin_name_args = _filter_keys_by_registered_commands(_PLUGIN_NAME_SUBCOMMANDS, canonical_names)
        config_key_args = _filter_keys_by_registered_commands(_CONFIG_KEY_SUBCOMMANDS, canonical_names)

        # Inject dynamic choice values from runtime context (config, registries)
        dynamic = _build_dynamic_completions(mng_ctx) if mng_ctx is not None else None
        if dynamic:
            for opt_key, data_key in _DYNAMIC_CHOICE_OPTIONS.items():
                cmd_name = opt_key.split(".")[0]
                if cmd_name in canonical_names and data_key in dynamic:
                    option_choices[opt_key] = dynamic[data_key]

        cache_data = CompletionCacheData(
            commands=all_command_names,
            aliases=alias_to_canonical,
            subcommand_by_command=subcommand_by_command,
            options_by_command=options_by_command,
            flag_options_by_command=flag_options_by_command,
            option_choices=option_choices,
            agent_name_arguments=sorted(agent_name_args),
            git_branch_options=sorted(git_branch_opts),
            host_name_options=sorted(host_name_opts),
            host_name_arguments=sorted(host_name_args),
            plugin_name_options=sorted(set(plugin_name_opts)),
            plugin_names=dynamic.get("plugin_names", []) if dynamic else [],
            plugin_name_arguments=sorted(plugin_name_args),
            config_key_arguments=sorted(config_key_args),
            config_keys=dynamic.get("config_keys", []) if dynamic else [],
        )

        cache_path = get_completion_cache_dir() / COMPLETION_CACHE_FILENAME
        atomic_write(cache_path, json.dumps(cache_data._asdict()))
    except OSError:
        logger.debug("Failed to write CLI completions cache")
