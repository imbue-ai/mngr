import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final
from typing import Sequence
from uuid import uuid4

import pluggy
from pydantic import BaseModel

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.mng.config.agent_config_registry import get_agent_config_class
from imbue.mng.config.consts import PROFILES_DIRNAME
from imbue.mng.config.consts import ROOT_CONFIG_FILENAME
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import CommandDefaults
from imbue.mng.config.data_types import CreateTemplate
from imbue.mng.config.data_types import CreateTemplateName
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import PluginConfig
from imbue.mng.config.data_types import ProviderInstanceConfig
from imbue.mng.config.data_types import split_cli_args_string
from imbue.mng.config.host_dir import read_default_host_dir
from imbue.mng.config.plugin_registry import get_plugin_config_class
from imbue.mng.config.pre_readers import find_profile_dir_lightweight
from imbue.mng.config.pre_readers import get_user_config_path
from imbue.mng.config.pre_readers import load_local_config
from imbue.mng.config.pre_readers import load_project_config
from imbue.mng.config.pre_readers import read_disabled_plugins
from imbue.mng.config.pre_readers import try_load_toml
from imbue.mng.config.provider_config_registry import get_provider_config_class
from imbue.mng.errors import ConfigParseError
from imbue.mng.errors import UnknownBackendError
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import PluginName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.utils.file_utils import atomic_write
from imbue.mng.utils.logging import LoggingConfig

# Environment variable prefix for command config overrides.
# Format: MNG_COMMANDS_<COMMANDNAME>_<VARNAME>=<value>
# Example: MNG_COMMANDS_CREATE_NEW_BRANCH_PREFIX=agent/
#
# IMPORTANT: Command names MUST be single words (no spaces, hyphens, or underscores).
# This is because we use the first underscore after "MNG_COMMANDS_" to separate
# the command name from the parameter name. If command names contained underscores,
# parsing would be ambiguous. For example, "MNG_COMMANDS_FOO_BAR_BAZ" could be:
#   - command="foo", param="bar_baz"
#   - command="foo_bar", param="baz"
#
# Any future plugins that register custom commands must follow this single-word rule.
_ENV_COMMANDS_PREFIX: Final[str] = "MNG_COMMANDS_"


def load_config(
    pm: pluggy.PluginManager,
    concurrency_group: ConcurrencyGroup,
    context_dir: Path | None = None,
    enabled_plugins: Sequence[str] | None = None,
    disabled_plugins: Sequence[str] | None = None,
    is_interactive: bool = False,
) -> MngContext:
    """Load and merge configuration from all sources.

    Precedence (lowest to highest):
    1. User config (~/.{root_name}/profiles/<profile_id>/settings.toml)
    2. Project config (.{root_name}/settings.toml at context_dir or git root)
    3. Local config (.{root_name}/settings.local.toml at context_dir or git root)
    4. Environment variables (MNG_ROOT_NAME, MNG_PREFIX, MNG_HOST_DIR)
    5. CLI arguments (handled by caller)

    MNG_ROOT_NAME is used to derive:
    1. Config file paths (where to look for settings files)
    2. Defaults for prefix and default_host_dir (if not set in config files)

    Explicit MNG_PREFIX/MNG_HOST_DIR values override MNG_ROOT_NAME-derived defaults.

    Returns MngContext containing both the final MngConfig and a reference to the plugin manager.
    """

    # Read MNG_ROOT_NAME early to use for config file discovery
    root_name = os.environ.get("MNG_ROOT_NAME", "mng")

    # Determine base directory (may be overridden by env var)
    base_dir = read_default_host_dir()

    # Get/create profile directory first (needed for user config
    profile_dir = get_or_create_profile_dir(base_dir)

    # Pre-compute disabled plugins so _parse_providers can skip them.
    # This uses the same lightweight pre-reader that create_plugin_manager() uses.
    config_disabled_plugins = read_disabled_plugins()

    # Start with base config that has defaults based on root_name
    # Use model_construct with None to allow merging to work properly
    config = MngConfig.model_construct(
        prefix=f"{root_name}-",
        default_host_dir=Path(f"~/.{root_name}"),
        agent_types={},
        providers={},
        plugins={},
        logging=LoggingConfig(),
        commands={"create": CommandDefaults(defaults={"pass_host_env": ["EDITOR"]})},
    )

    # Load and merge config files in precedence order (user, project, local)
    for raw in (
        try_load_toml(get_user_config_path(profile_dir)),
        load_project_config(context_dir, root_name, concurrency_group),
        load_local_config(context_dir, root_name, concurrency_group),
    ):
        if raw is not None:
            config = config.merge_with(parse_config(raw, disabled_plugins=config_disabled_plugins))

    # Apply environment variable overrides
    prefix = os.environ.get("MNG_PREFIX")
    default_host_dir = os.environ.get("MNG_HOST_DIR")

    # Build a dict with non-None values for final validation
    config_dict: dict[str, Any] = {}

    # Apply env var overrides, or use merged values
    if prefix is not None:
        config_dict["prefix"] = prefix
    elif config.prefix is not None:
        config_dict["prefix"] = config.prefix
    else:
        # Neither env var nor config has prefix - will use pydantic default
        pass

    if default_host_dir is not None:
        config_dict["default_host_dir"] = Path(default_host_dir)
    elif config.default_host_dir is not None:
        config_dict["default_host_dir"] = config.default_host_dir
    else:
        # Neither env var nor config has default_host_dir - will use pydantic default
        pass

    # Always include agent_types, providers, plugins, commands, and create_templates (they default to empty dicts)
    config_dict["agent_types"] = config.agent_types
    config_dict["providers"] = config.providers
    config_dict["plugins"] = config.plugins
    config_dict["commands"] = config.commands
    config_dict["create_templates"] = config.create_templates

    # Apply environment variable overrides for commands
    # Format: MNG_COMMANDS_<COMMANDNAME>_<PARAMNAME>=<value>
    # See _ENV_COMMANDS_PREFIX comment for details on the single-word command name requirement
    env_command_overrides = _parse_command_env_vars(os.environ)
    if env_command_overrides:
        config_dict["commands"] = _merge_command_defaults(
            config_dict["commands"],
            env_command_overrides,
        )

    # Apply CLI plugin overrides
    config_dict["plugins"], config_dict["disabled_plugins"] = _apply_plugin_overrides(
        config_dict["plugins"],
        enabled_plugins,
        disabled_plugins,
    )

    # Block disabled plugins so their hooks don't fire. This covers
    # CLI-level --disable-plugin flags that weren't known at startup.
    block_disabled_plugins(pm, config_dict["disabled_plugins"])

    # Include logging if not None
    if config.logging is not None:
        config_dict["logging"] = config.logging

    config_dict["is_nested_tmux_allowed"] = config.is_nested_tmux_allowed
    config_dict["is_error_reporting_enabled"] = config.is_error_reporting_enabled
    config_dict["is_allowed_in_pytest"] = config.is_allowed_in_pytest
    config_dict["pre_command_scripts"] = config.pre_command_scripts
    config_dict["default_destroyed_host_persisted_seconds"] = config.default_destroyed_host_persisted_seconds

    # Allow plugins to modify config_dict before validation
    pm.hook.on_load_config(config_dict=config_dict)

    # Validate and apply defaults using normal constructor
    final_config = MngConfig.model_validate(config_dict)

    # check whether we're in pytest
    if not final_config.is_allowed_in_pytest:
        if "PYTEST_CURRENT_TEST" in os.environ:
            raise ConfigParseError(
                "Running mng within pytest is not allowed by the current configuration. This can happen when tests are poorly written, and load the .mng/settings.toml file from the root of the mng project"
            )

    # Return MngContext containing both config and plugin manager
    return MngContext(
        config=final_config,
        pm=pm,
        is_interactive=is_interactive,
        profile_dir=profile_dir,
        concurrency_group=concurrency_group,
    )


def get_or_create_profile_dir(base_dir: Path) -> Path:
    """Get or create the profile directory for this mng installation.

    The profile directory is stored at ~/.mng/profiles/<profile_id>/. The active
    profile is specified in ~/.mng/config.toml. If no profile exists, a new one
    is created with a generated profile ID and saved to config.toml.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    profiles_dir = base_dir / PROFILES_DIRNAME
    profiles_dir.mkdir(parents=True, exist_ok=True)

    # Try read-only lookup first
    existing = find_profile_dir_lightweight(base_dir)
    if existing is not None:
        return existing

    # Config specifies a profile ID but the directory doesn't exist yet -- create it
    config_path = base_dir / ROOT_CONFIG_FILENAME
    if config_path.exists():
        try:
            with open(config_path, "rb") as f:
                root_config = tomllib.load(f)
            profile_id = root_config.get("profile")
            if profile_id:
                profile_dir = profiles_dir / profile_id
                profile_dir.mkdir(parents=True, exist_ok=True)
                return profile_dir
        except tomllib.TOMLDecodeError:
            pass

    # No valid config.toml or no profile specified -- create a new profile
    profile_id = uuid4().hex
    profile_dir = profiles_dir / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)

    atomic_write(config_path, f'profile = "{profile_id}"\n')

    return profile_dir


# =============================================================================
# Config Loading
# =============================================================================


def _check_unknown_fields(
    raw_config: dict[str, Any],
    model_class: type[BaseModel],
    context: str,
) -> None:
    """Raise ConfigParseError if raw_config contains fields not defined on model_class.

    This catches typos and misconfigured keys early rather than silently ignoring them,
    which is important because model_construct bypasses pydantic's extra="forbid" validation.
    """
    known_fields = set(model_class.model_fields.keys())
    unknown = set(raw_config.keys()) - known_fields
    if unknown:
        raise ConfigParseError(f"Unknown fields in {context}: {sorted(unknown)}. Valid fields: {sorted(known_fields)}")


def _parse_providers(
    raw_providers: dict[str, dict[str, Any]],
    disabled_plugins: frozenset[str],
) -> dict[ProviderInstanceName, ProviderInstanceConfig]:
    """Parse provider configs using the registry.

    Uses model_construct to bypass validation and explicitly set None for unset fields.
    Provider blocks whose plugin is disabled are silently skipped.
    """
    providers: dict[ProviderInstanceName, ProviderInstanceConfig] = {}

    for name, raw_config in raw_providers.items():
        backend = raw_config.get("backend") or name
        plugin = raw_config.get("plugin") or backend
        if plugin in disabled_plugins:
            continue
        try:
            config_class = get_provider_config_class(backend)
        except UnknownBackendError as e:
            msg = f"Provider '{name}' references unknown backend '{backend}'."
            if disabled_plugins:
                msg += (
                    f" If this backend is provided by a disabled plugin, either enable"
                    f' the plugin or add `plugin = "<plugin-name>"` to this provider'
                    f" block. Currently disabled plugins: {', '.join(sorted(disabled_plugins))}"
                )
            raise ConfigParseError(msg) from e
        _check_unknown_fields(raw_config, config_class, f"providers.{name}")
        providers[ProviderInstanceName(name)] = config_class.model_construct(**raw_config)

    return providers


def _normalize_cli_args_for_construct(raw_config: dict[str, Any]) -> dict[str, Any]:
    """Normalize cli_args from str or list to tuple before model_construct (which bypasses validators)."""
    if "cli_args" not in raw_config:
        return raw_config
    cli_args = raw_config["cli_args"]
    if isinstance(cli_args, str):
        normalized = split_cli_args_string(cli_args) if cli_args else ()
    elif isinstance(cli_args, (list, tuple)):
        normalized = tuple(cli_args)
    else:
        normalized = cli_args
    return {**raw_config, "cli_args": normalized}


def _parse_agent_types(
    raw_types: dict[str, dict[str, Any]],
) -> dict[AgentTypeName, AgentTypeConfig]:
    """Parse agent type configs using the registry.

    Uses model_construct to bypass validation and explicitly set None for unset fields.
    """
    agent_types: dict[AgentTypeName, AgentTypeConfig] = {}

    for name, raw_config in raw_types.items():
        config_class = get_agent_config_class(name)
        _check_unknown_fields(raw_config, config_class, f"agent_types.{name}")
        normalized_config = _normalize_cli_args_for_construct(raw_config)
        agent_types[AgentTypeName(name)] = config_class.model_construct(**normalized_config)

    return agent_types


def _parse_plugins(
    raw_plugins: dict[str, dict[str, Any]],
) -> dict[PluginName, PluginConfig]:
    """Parse plugin configs using the registry.

    Uses model_construct to bypass validation and explicitly set None for unset fields.
    """
    plugins: dict[PluginName, PluginConfig] = {}

    for name, raw_config in raw_plugins.items():
        config_class = get_plugin_config_class(name)
        _check_unknown_fields(raw_config, config_class, f"plugins.{name}")
        plugins[PluginName(name)] = config_class.model_construct(**raw_config)

    return plugins


def _apply_plugin_overrides(
    plugins: dict[PluginName, PluginConfig],
    enabled_plugins: Sequence[str] | None,
    disabled_plugins: Sequence[str] | None,
) -> tuple[dict[PluginName, PluginConfig], frozenset[str]]:
    """Apply CLI plugin enable/disable overrides and filter out disabled plugins.

    Returns a tuple of (enabled_plugins_dict, disabled_plugin_names).
    """
    # Create a mutable copy
    result: dict[PluginName, PluginConfig] = dict(plugins)

    # Apply enabled plugins (add if not present, or set enabled=True)
    if enabled_plugins:
        for plugin_name_str in enabled_plugins:
            plugin_name = PluginName(plugin_name_str)
            if plugin_name in result:
                # Plugin exists - set enabled=True
                existing = result[plugin_name]
                result[plugin_name] = existing.model_copy_update(
                    to_update(existing.field_ref().enabled, True),
                )
            else:
                # Plugin doesn't exist - create with enabled=True
                config_class = get_plugin_config_class(plugin_name_str)
                result[plugin_name] = config_class(enabled=True)

    # Apply disabled plugins (set enabled=False)
    if disabled_plugins:
        for plugin_name_str in disabled_plugins:
            plugin_name = PluginName(plugin_name_str)
            if plugin_name in result:
                # Plugin exists - set enabled=False
                existing = result[plugin_name]
                result[plugin_name] = existing.model_copy_update(
                    to_update(existing.field_ref().enabled, False),
                )
            else:
                # Plugin doesn't exist - create with enabled=False
                config_class = get_plugin_config_class(plugin_name_str)
                result[plugin_name] = config_class(enabled=False)

    # Collect disabled plugin names and filter out disabled plugins
    disabled_names = frozenset(str(name) for name, config in result.items() if not config.enabled)
    enabled_result = {name: config for name, config in result.items() if config.enabled}
    return enabled_result, disabled_names


def block_disabled_plugins(pm: pluggy.PluginManager, disabled_names: frozenset[str]) -> None:
    """Block disabled plugins in the plugin manager so their hooks don't fire.

    Uses pm.set_blocked() which both prevents future registration and
    unregisters already-registered plugins. Safe to call for names that
    are already blocked (no-op in that case).
    """
    for name in disabled_names:
        if not pm.is_blocked(name):
            pm.set_blocked(name)


def _parse_logging_config(raw_logging: dict[str, Any]) -> LoggingConfig:
    """Parse logging config.

    Uses model_construct to bypass validation and explicitly set None for unset fields.
    """
    _check_unknown_fields(raw_logging, LoggingConfig, "logging")
    return LoggingConfig.model_construct(**raw_logging)


def _parse_commands(raw_commands: dict[str, dict[str, Any]]) -> dict[str, CommandDefaults]:
    """Parse command defaults from config.

    Format: commands.{command_name}.{param_name} = value
    Example: [commands.create]
             new_host = "docker"
             connect = false

    The special key `default_subcommand` is extracted separately from the
    parameter defaults dict so it can be stored on CommandDefaults as a
    first-class field.

    Uses model_construct to bypass validation and explicitly set None for unset fields.
    """
    commands: dict[str, CommandDefaults] = {}

    for command_name, raw_defaults in raw_commands.items():
        # Make a mutable copy so we don't mutate the caller's dict
        defaults_copy = dict(raw_defaults)
        default_subcommand = defaults_copy.pop("default_subcommand", None)
        commands[command_name] = CommandDefaults.model_construct(
            defaults=defaults_copy,
            default_subcommand=default_subcommand,
        )

    return commands


def _parse_create_templates(raw_templates: dict[str, dict[str, Any]]) -> dict[CreateTemplateName, CreateTemplate]:
    """Parse create templates from config.

    Format: create_templates.{template_name}.{param_name} = value
    Example: [create_templates.modal-dev]
             new_host = "modal"
             target_path = "/root/workspace"

    Uses model_construct to bypass validation and explicitly set None for unset fields.
    """
    templates: dict[CreateTemplateName, CreateTemplate] = {}

    for template_name, raw_options in raw_templates.items():
        templates[CreateTemplateName(template_name)] = CreateTemplate.model_construct(options=raw_options)

    return templates


def parse_config(
    raw: dict[str, Any],
    disabled_plugins: frozenset[str],
) -> MngConfig:
    """Parse a raw config dict into MngConfig.

    Uses model_construct to bypass defaults and explicitly set None for unset fields.
    """
    # Build kwargs with None for unset scalar fields
    kwargs: dict[str, Any] = {}
    kwargs["prefix"] = raw.pop("prefix", None)
    kwargs["default_host_dir"] = raw.pop("default_host_dir", None)
    kwargs["agent_types"] = _parse_agent_types(raw.pop("agent_types", {})) if "agent_types" in raw else {}
    kwargs["providers"] = (
        _parse_providers(raw.pop("providers", {}), disabled_plugins=disabled_plugins) if "providers" in raw else {}
    )
    kwargs["plugins"] = _parse_plugins(raw.pop("plugins", {})) if "plugins" in raw else {}
    kwargs["commands"] = _parse_commands(raw.pop("commands", {})) if "commands" in raw else {}
    kwargs["create_templates"] = (
        _parse_create_templates(raw.pop("create_templates", {})) if "create_templates" in raw else {}
    )
    kwargs["logging"] = _parse_logging_config(raw.pop("logging", {})) if "logging" in raw else None
    kwargs["is_nested_tmux_allowed"] = (
        raw.pop("is_nested_tmux_allowed", None) if "is_nested_tmux_allowed" in raw else None
    )
    kwargs["is_error_reporting_enabled"] = (
        raw.pop("is_error_reporting_enabled", None) if "is_error_reporting_enabled" in raw else None
    )
    kwargs["is_allowed_in_pytest"] = raw.pop("is_allowed_in_pytest", {}) if "is_allowed_in_pytest" in raw else None
    kwargs["pre_command_scripts"] = raw.pop("pre_command_scripts", {}) if "pre_command_scripts" in raw else None
    kwargs["default_destroyed_host_persisted_seconds"] = raw.pop("default_destroyed_host_persisted_seconds", None)

    if len(raw) > 0:
        raise ConfigParseError(f"Unknown configuration fields: {list(raw.keys())}")

    # Use model_construct to bypass field defaults
    return MngConfig.model_construct(**kwargs)


# =============================================================================
# Environment Variable Overrides for Commands
# =============================================================================


def _parse_command_env_vars(environ: Mapping[str, str]) -> dict[str, CommandDefaults]:
    """Parse environment variables to extract command config overrides.

    Looks for environment variables matching the pattern:
        MNG_COMMANDS_<COMMANDNAME>_<PARAMNAME>=<value>

    where:
        - COMMANDNAME is the command name in uppercase (e.g., CREATE, LIST)
        - PARAMNAME is the parameter name in uppercase with underscores (e.g., NEW_BRANCH_PREFIX)
        - value is the string value to set

    The command name is determined by the first underscore after "MNG_COMMANDS_".
    The remaining part becomes the parameter name (lowercased).

    IMPORTANT: Command names MUST be single words (no underscores) for unambiguous parsing.
    See the comment at _ENV_COMMANDS_PREFIX for details.

    Examples:
        MNG_COMMANDS_CREATE_NEW_BRANCH_PREFIX=agent/
            -> commands["create"]["new_branch_prefix"] = "agent/"

        MNG_COMMANDS_CREATE_CONNECT=false
            -> commands["create"]["connect"] = "false"

        MNG_COMMANDS_LIST_FORMAT=json
            -> commands["list"]["format"] = "json"

    Returns:
        Dict mapping command names to CommandDefaults with the parsed values.
    """
    commands: dict[str, dict[str, Any]] = {}

    for env_key, env_value in environ.items():
        if not env_key.startswith(_ENV_COMMANDS_PREFIX):
            continue

        # Strip the prefix to get "<COMMANDNAME>_<PARAMNAME>"
        suffix = env_key[len(_ENV_COMMANDS_PREFIX) :]
        if not suffix:
            continue

        # Find the first underscore to split command name from param name
        underscore_idx = suffix.find("_")
        if underscore_idx == -1:
            # No underscore means no param name, skip this
            continue

        command_name = suffix[:underscore_idx].lower()
        param_name = suffix[underscore_idx + 1 :].lower()

        if not command_name or not param_name:
            continue

        # Initialize the command's dict if needed
        if command_name not in commands:
            commands[command_name] = {}

        # Store as string - type conversion happens downstream in click/pydantic
        # where the actual type information is available
        commands[command_name][param_name] = env_value

    # Convert raw dicts to CommandDefaults
    result: dict[str, CommandDefaults] = {}
    for command_name, params in commands.items():
        result[command_name] = CommandDefaults(defaults=params)

    return result


def _merge_command_defaults(
    base: dict[str, CommandDefaults],
    override: dict[str, CommandDefaults],
) -> dict[str, CommandDefaults]:
    """Merge two command defaults dicts, with override taking precedence."""
    result: dict[str, CommandDefaults] = dict(base)

    for command_name, override_defaults in override.items():
        if command_name in result:
            result[command_name] = result[command_name].merge_with(override_defaults)
        else:
            result[command_name] = override_defaults

    return result
