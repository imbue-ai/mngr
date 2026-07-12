"""Integration tests for the tab completion cache.

These tests run write_cli_completions_cache against the real CLI group to
verify that hand-maintained completion constants reference options that
actually exist. This catches renames (e.g. --base-branch -> --branch)
that unit tests with hand-crafted data miss.
"""

import json
from pathlib import Path

import click

from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.agent_registry import list_registered_agent_types
from imbue.mngr.cli.command_names import KNOWN_CONFIG_COMMAND_NAMES
from imbue.mngr.cli.command_names import build_default_subcommand_choices
from imbue.mngr.cli.help_topics import get_all_topics
from imbue.mngr.config.completion_cache import COMPLETION_CACHE_FILENAME
from imbue.mngr.config.completion_cache import CompletionCacheData
from imbue.mngr.config.completion_writer import write_cli_completions_cache
from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import CreateTemplate
from imbue.mngr.config.data_types import CreateTemplateName
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.main import cli
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName


def _read_cache(cache_dir: Path) -> CompletionCacheData:
    data = json.loads((cache_dir / COMPLETION_CACHE_FILENAME).read_text())
    return CompletionCacheData(**{k: v for k, v in data.items() if k in CompletionCacheData._fields})


def _assert_option_exists_on_cli(dotted_key: str, label: str) -> None:
    """Assert that a dotted key like "create.--host" references a real CLI option."""
    parts = dotted_key.split(".")
    option_name = parts[-1]
    assert option_name.startswith("--"), f"Unexpected key format in {label}: {dotted_key}"

    cmd = cli
    for part in parts[:-1]:
        assert isinstance(cmd, click.Group) and part in cmd.commands, (
            f"{label} key {dotted_key!r} references command {part!r} which does not exist"
        )
        cmd = cmd.commands[part]

    option_names = set()
    for param in cmd.params:
        if hasattr(param, "opts"):
            option_names.update(param.opts)
            option_names.update(param.secondary_opts)
    assert option_name in option_names, (
        f"{label} key {dotted_key!r} references {option_name!r} "
        f"which does not exist. Available: {sorted(option_names)}"
    )


def test_option_choices_reference_real_options(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Every option_choices key must reference an option that exists on the real CLI."""
    write_cli_completions_cache(
        cli_group=cli, mngr_ctx=temp_mngr_ctx, registered_agent_types=list_registered_agent_types()
    )
    cache = _read_cache(completion_cache_dir)

    for choice_key in cache.option_choices:
        _assert_option_exists_on_cli(choice_key, "option_choices")


def test_git_branch_options_reference_real_options(completion_cache_dir: Path) -> None:
    """Every git_branch_options key must reference an option that exists on the real CLI."""
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    for key in cache.git_branch_options:
        _assert_option_exists_on_cli(key, "git_branch_options")


def test_host_name_options_reference_real_options(completion_cache_dir: Path) -> None:
    """Every host_name_options key must reference an option that exists on the real CLI."""
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    for key in cache.host_name_options:
        _assert_option_exists_on_cli(key, "host_name_options")


def test_plugin_name_options_reference_real_options(completion_cache_dir: Path) -> None:
    """Every plugin_name_options key must reference an option that exists on the real CLI."""
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    for key in cache.plugin_name_options:
        _assert_option_exists_on_cli(key, "plugin_name_options")


def _collect_all_options_from_cli() -> dict[str, set[str]]:
    """Walk the real CLI tree and collect all --long options keyed by dotted command path.

    Returns a dict mapping command key (e.g. "create", "config.set") to the set
    of --long option names on that command.
    """
    result: dict[str, set[str]] = {}
    assert isinstance(cli, click.Group)
    for name, cmd in cli.commands.items():
        if isinstance(cmd, click.Group) and cmd.commands:
            for sub_name, sub_cmd in cmd.commands.items():
                key = f"{cmd.name or name}.{sub_name}"
                opts: set[str] = set()
                for param in sub_cmd.params:
                    if isinstance(param, click.Option):
                        for opt in param.opts + param.secondary_opts:
                            if opt.startswith("--"):
                                opts.add(opt)
                if opts:
                    result[key] = opts
            # Also collect group-level options
            group_opts: set[str] = set()
            for param in cmd.params:
                if isinstance(param, click.Option):
                    for opt in param.opts + param.secondary_opts:
                        if opt.startswith("--"):
                            group_opts.add(opt)
            if group_opts:
                result[cmd.name or name] = group_opts
        else:
            key = cmd.name or name
            opts = set()
            for param in cmd.params:
                if isinstance(param, click.Option):
                    for opt in param.opts + param.secondary_opts:
                        if opt.startswith("--"):
                            opts.add(opt)
            if opts:
                result[key] = opts
    return result


def test_help_targets_cover_commands_and_topics(completion_cache_dir: Path) -> None:
    """`mngr help` positional completion offers every top-level command and help topic.

    Exercises the real CLI: help_targets must include the command names and the
    topic keys passed in, and the help command must be wired to use them.
    """
    topic_names = sorted(get_all_topics().keys())
    write_cli_completions_cache(cli_group=cli, topic_names=topic_names)
    cache = _read_cache(completion_cache_dir)

    # The help command is wired to complete against the help_targets source.
    assert cache.positional_completions.get("help") == [["help_targets"]]

    # Every top-level command (e.g. create, destroy) is a candidate.
    for command_name in ("create", "destroy", "help"):
        assert command_name in cache.help_targets, f"{command_name!r} missing from help_targets"

    # Every registered topic (e.g. the built-in address topic) is a candidate.
    assert "address" in cache.help_targets
    for topic_name in topic_names:
        assert topic_name in cache.help_targets, f"topic {topic_name!r} missing from help_targets"


def test_help_targets_absent_without_topic_names(completion_cache_dir: Path) -> None:
    """Without passed-in topic names, help_targets still covers commands (topics just absent)."""
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    assert "create" in cache.help_targets
    assert "address" not in cache.help_targets


def test_setting_option_names_reference_real_options(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """The recorded setting option names must be real options, and config data must be present.

    ``-S``/``--setting`` is a global common option added by ``add_common_options``;
    this catches a rename of that option (which would silently break ``-S KEY=VALUE``
    completion) and confirms the KEY/VALUE data the completer relies on is populated.
    """
    write_cli_completions_cache(
        cli_group=cli, mngr_ctx=temp_mngr_ctx, registered_agent_types=list_registered_agent_types()
    )
    cache = _read_cache(completion_cache_dir)

    assert cache.setting_option_names == ["--setting", "-S"]

    # Both forms must actually exist on a command that carries the common options.
    create_cmd = cli.commands["create"]
    option_names: set[str] = set()
    for param in create_cmd.params:
        if hasattr(param, "opts"):
            option_names.update(param.opts)
            option_names.update(param.secondary_opts)
    for setting_option in cache.setting_option_names:
        assert setting_option in option_names, (
            f"setting option {setting_option!r} does not exist on `create`. Available: {sorted(option_names)}"
        )

    # The KEY=VALUE completer reuses the config key/value data, so it must be populated.
    assert cache.config_keys, "config_keys is empty; -S key completion would offer nothing"
    assert cache.config_value_choices, "config_value_choices is empty; -S value completion would offer nothing"


def test_builtin_agent_type_keys_are_completed_from_schema(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Builtin/registered agent types contribute agent_types.<name>.* keys, not just custom ones.

    These are derived from each type's config *schema*, so a builtin type like
    ``claude`` (which is not present in the user's config) still completes its
    settable fields, and constrained fields complete their values
    (``parent_type`` -> agent type names, bool fields -> true/false).
    """
    # A registered builtin type that is NOT present in the user's config. (Plugin
    # types like claude are not loaded in this test env, so use an explicit
    # builtin name; get_agent_config_class falls back to the base AgentTypeConfig.)
    builtin = "my-builtin-agent"
    assert builtin not in temp_mngr_ctx.config.agent_types

    write_cli_completions_cache(cli_group=cli, mngr_ctx=temp_mngr_ctx, registered_agent_types=[builtin])
    cache = _read_cache(completion_cache_dir)

    # Its settable fields are offered as keys, derived from the config schema.
    assert f"agent_types.{builtin}.command" in cache.config_keys
    assert f"agent_types.{builtin}.parent_type" in cache.config_keys

    # parent_type (an AgentTypeName field) completes to the known agent type names,
    # which include the registered builtin.
    parent_type_choices = cache.config_value_choices.get(f"agent_types.{builtin}.parent_type")
    assert parent_type_choices is not None
    assert builtin in parent_type_choices


def test_options_record_both_forms_and_classify_no_value_options(completion_cache_dir: Path) -> None:
    """options_by_command holds both forms of every option; flag_options holds the no-value ones.

    The positional-argument counter checks flag_options first (consume 1 word) and
    otherwise consumes a recognised option's value (2 words). So value-taking
    options (e.g. ``-S``/``--setting``) must be in options_by_command but not
    flag_options, while no-value options (flags and count options like
    ``-v``/``--verbose``) must be in flag_options -- both long and short forms,
    treated uniformly.
    """
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    create_options = cache.options_by_command["create"]
    create_flags = cache.flag_options_by_command["create"]

    # -S/--setting is value-taking: both forms recognised, neither is a no-value option.
    assert {"-S", "--setting"} <= set(create_options)
    assert "-S" not in create_flags
    assert "--setting" not in create_flags

    # -v/--verbose is a count option (no value): both forms recognised AND both
    # classified as no-value, so the counter consumes only the option word.
    assert {"-v", "--verbose"} <= set(create_options)
    assert {"-v", "--verbose"} <= set(create_flags)


def test_every_option_is_classified(completion_cache_dir: Path) -> None:
    """Every CLI --long option must appear in options_by_command in the cache.

    This catches options added to commands without updating the cache writer,
    and renames (e.g. --agent-type -> --type) that would go undetected.
    """
    write_cli_completions_cache(cli_group=cli)
    cache = _read_cache(completion_cache_dir)

    cli_options = _collect_all_options_from_cli()
    missing: list[str] = []

    for command_key, option_names in cli_options.items():
        cached_options = set(cache.options_by_command.get(command_key, []))
        for opt in sorted(option_names):
            if opt not in cached_options:
                missing.append(f"{command_key}.{opt}")

    assert not missing, "The following CLI options are not in options_by_command in the cache:\n" + "\n".join(
        f"  {m}" for m in missing
    )


def _schema_cache(completion_cache_dir: Path, mngr_ctx: MngrContext) -> CompletionCacheData:
    """Write and read the cache for ``mngr_ctx`` with the full command-name registry.

    The registry is what lets ``commands.*`` / ``pre_command_scripts.*`` keys be
    offered for command buckets that are not yet configured, so pass it just as
    the ``list`` command does.
    """
    write_cli_completions_cache(
        cli_group=cli,
        mngr_ctx=mngr_ctx,
        registered_agent_types=list_registered_agent_types(),
        config_command_names=sorted(KNOWN_CONFIG_COMMAND_NAMES),
        default_subcommand_choices=build_default_subcommand_choices(cli),
    )
    return _read_cache(completion_cache_dir)


def test_command_defaults_keys_are_schema_discoverable(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """``commands.<cmd>.<param>`` keys are offered before anything is configured.

    Options complete from the command's click params (not from a flattened config
    instance), so a param under an unconfigured command is discoverable, and a
    boolean option completes ``true``/``false``. Group subcommands are namespaced
    ``<group>_<sub>`` to match their ``command_name`` bucket.
    """
    assert "destroy" not in temp_mngr_ctx.config.commands
    cache = _schema_cache(completion_cache_dir, temp_mngr_ctx)

    # A top-level command's option is discoverable pre-config, and a boolean
    # option (``--headless``, on every command) completes its value.
    assert "commands.destroy.headless" in cache.config_keys
    assert cache.config_value_choices.get("commands.destroy.headless") == ["true", "false"]

    # A group subcommand uses the ``<group>_<sub>`` bucket name.
    assert "commands.snapshot_create.headless" in cache.config_keys

    # The group itself owns no per-command param bucket (only its subcommands do),
    # so no ``commands.snapshot.<param>`` param keys are fabricated.
    assert not any(
        key.startswith("commands.snapshot.") and not key.endswith(".default_subcommand")
        for key in cache.config_keys
    )
    # And the group-level ``config`` / ``plugin`` buckets, whose derived
    # ``<group>_<sub>`` names are absent from the registry, contribute no params.
    assert "commands.config_set.headless" not in cache.config_keys


def test_default_subcommand_keys_come_from_group_config_keys(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Each ``DefaultCommandGroup`` offers ``commands.<config_key>.default_subcommand``.

    The value choices are the group's canonical subcommand names (aliases are
    excluded, since a default is best chosen by canonical name).
    """
    cache = _schema_cache(completion_cache_dir, temp_mngr_ctx)

    assert "commands.snapshot.default_subcommand" in cache.config_keys
    assert cache.config_value_choices.get("commands.snapshot.default_subcommand") == ["create", "destroy", "list"]

    # The top-level group's default subcommand completes to canonical command
    # names; alias entries (e.g. ``ls`` for ``list``) are filtered out.
    mngr_choices = cache.config_value_choices.get("commands.mngr.default_subcommand")
    assert mngr_choices is not None
    assert "create" in mngr_choices
    assert "list" in mngr_choices
    assert "ls" not in mngr_choices


def test_pre_command_scripts_keys_cover_registry(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Every ``command_name`` bucket is offered as a ``pre_command_scripts.<name>`` key.

    These are complete leaf keys (the value is a free-form list of scripts), so
    the whole set is discoverable before anything is configured, including the
    ``<group>_<sub>`` subcommand buckets.
    """
    cache = _schema_cache(completion_cache_dir, temp_mngr_ctx)

    expected = {f"pre_command_scripts.{name}" for name in KNOWN_CONFIG_COMMAND_NAMES}
    assert expected <= set(cache.config_keys)
    # Spot-check the namespaced subcommand buckets specifically.
    assert "pre_command_scripts.snapshot_create" in cache.config_keys
    assert "pre_command_scripts.git_pull" in cache.config_keys


def test_provider_instance_keys_come_from_schema(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A configured provider exposes all settable fields, not just the ones it set.

    Provider instance names are user-chosen, so only configured providers appear
    -- but each is walked from ``ProviderInstanceConfig``'s schema, so an unset
    field is still offered and ``backend`` completes to the known backend names.
    """
    config = temp_mngr_ctx.config.model_copy_update(
        to_update(
            temp_mngr_ctx.config.field_ref().providers,
            {ProviderInstanceName("myprov"): ProviderInstanceConfig(backend=ProviderBackendName("local"))},
        )
    )
    mngr_ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, config))
    cache = _schema_cache(completion_cache_dir, mngr_ctx)

    # An unset field of the configured provider is discoverable from the schema.
    assert "providers.myprov.discovery_poll_interval_seconds" in cache.config_keys
    # backend (a ProviderBackendName field) completes to the registered backends
    # (only builtin backends are registered in the test env; plugin backends like
    # docker are not loaded).
    backend_choices = cache.config_value_choices.get("providers.myprov.backend")
    assert backend_choices is not None
    assert "local" in backend_choices


def test_plugin_keys_come_from_schema(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """A configured plugin's fields complete from the ``PluginConfig`` schema."""
    config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().plugins, {PluginName("claude"): PluginConfig(enabled=True)})
    )
    mngr_ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, config))
    cache = _schema_cache(completion_cache_dir, mngr_ctx)

    assert "plugins.claude.enabled" in cache.config_keys
    assert cache.config_value_choices.get("plugins.claude.enabled") == ["true", "false"]


def test_configured_command_and_template_params_use_unwrapped_keys(
    completion_cache_dir: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Configured ``commands`` / ``create_templates`` params use the user-facing key shape.

    Users set ``commands.<cmd>.<param>`` and ``create_templates.<name>.<param>``;
    the internal ``defaults`` / ``options`` wrappers must never leak into the
    completion keys (those don't round-trip through ``config set``). Templates
    additionally expose all ``CreateCliOptions`` fields, so an unset one is
    discoverable once the template exists.
    """
    config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().commands, {"create": CommandDefaults(defaults={"branch": "main"})}),
        to_update(
            temp_mngr_ctx.config.field_ref().create_templates,
            {CreateTemplateName("dev"): CreateTemplate(options={"new_host": True})},
        ),
    )
    mngr_ctx = temp_mngr_ctx.model_copy_update(to_update(temp_mngr_ctx.field_ref().config, config))
    cache = _schema_cache(completion_cache_dir, mngr_ctx)

    assert "commands.create.branch" in cache.config_keys
    assert "create_templates.dev.new_host" in cache.config_keys
    # An unset template option is still discoverable from CreateCliOptions.
    assert "create_templates.dev.target_path" in cache.config_keys

    # A template option completes to the same values as the create command's
    # option: ``type`` -> agent type names (a builtin like ``command``).
    type_choices = cache.config_value_choices.get("create_templates.dev.type")
    assert type_choices is not None
    assert "command" in type_choices

    # The internal wrapper segments must never appear.
    assert not any(".defaults." in key for key in cache.config_keys)
    assert not any(".options." in key for key in cache.config_keys)
