import string
import sys
import uuid
from collections.abc import Callable
from collections.abc import Sequence
from concurrent.futures import Future
from pathlib import Path
from typing import Any
from typing import TypeVar

import click
import pluggy
from click.core import ParameterSource
from click_option_group import GroupedOption
from click_option_group import OptionGroup
from click_option_group import optgroup
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import CreateTemplateName
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.config.key_resolver import resolve_extends
from imbue.mngr.config.key_resolver import set_at_path
from imbue.mngr.config.loader import block_disabled_plugins
from imbue.mngr.config.loader import load_config
from imbue.mngr.config.loader import parse_config
from imbue.mngr.config.loader import resolve_strict_from_env
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import ParseSpecError
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.utils.logging import LoggingConfig
from imbue.mngr.utils.logging import setup_logging
from imbue.mngr.utils.thread_cleanup import mngr_executor
from imbue.overlay.errors import OverlayError
from imbue.overlay.narrowing import would_assignment_narrow
from imbue.overlay.node_merge import extend_plain_value
from imbue.overlay.operators import bare_key
from imbue.overlay.operators import is_extend_key
from imbue.overlay.operators import parse_scalar_value

# The set of built-in format names (case-insensitive). Any --format value not
# matching one of these is treated as a format template string.
_BUILTIN_FORMAT_NAMES: frozenset[str] = frozenset(f.value.lower() for f in OutputFormat)

# Constant for the "Common" option group name used across all commands
COMMON_OPTIONS_GROUP_NAME = "Common"

TCommandOptions = TypeVar("TCommandOptions", bound="CommonCliOptions")
TDecorated = TypeVar("TDecorated", bound=Callable[..., Any])
TCommand = TypeVar("TCommand", bound=click.Command)


def add_common_options(command: TDecorated) -> TDecorated:
    """Decorator to add common options to a command.

    Adds the following options in the "Common" option group:
    - --format: Output format (human/json/jsonl, or a template string)
    - -q, --quiet: Suppress console output
    - -v, --verbose: Increase verbosity
    - --log-file: Override log file path
    - --log-commands: Log executed commands
    - --headless: Disable all interactive behavior
    - --plugin: Enable plugins
    - --disable-plugin: Disable plugins
    - -S, --setting: Override config settings for this invocation (KEY=VALUE, dot-separated
      paths; a trailing ``__extend`` on the leaf key opts into the list/dict/set extend
      operator)
    """
    # Apply decorators in reverse order (bottom to top)
    # These are wrapped in the "Common" option group
    command = optgroup.option(
        "-S",
        "--setting",
        multiple=True,
        help=(
            "Override a config setting for this invocation (KEY=VALUE, dot-separated paths; "
            "append __extend to the leaf key to extend list/dict/set fields) [repeatable]"
        ),
    )(command)
    command = optgroup.option("--disable-plugin", multiple=True, help="Disable a plugin [repeatable]")(command)
    command = optgroup.option("--plugin", "--enable-plugin", multiple=True, help="Enable a plugin [repeatable]")(
        command
    )
    command = optgroup.option(
        "--safe",
        is_flag=True,
        default=False,
        help="Always query all providers during discovery (disable event-stream optimization). "
        "Use this when interfacing with mngr from multiple machines.",
    )(command)
    command = optgroup.option(
        "--headless",
        is_flag=True,
        default=False,
        help="Disable all interactive behavior (prompts, TUI, editor). Also settable via MNGR_HEADLESS env var or 'headless' config key.",
    )(command)
    command = optgroup.option(
        "--log-commands/--no-log-commands", default=None, help="Log commands that were executed"
    )(command)
    command = optgroup.option(
        "--log-file",
        type=click.Path(),
        default=None,
        help="Path to log file (overrides default ~/.mngr/events/logs/<timestamp>-<pid>.json)",
    )(command)
    command = optgroup.option(
        "-v", "--verbose", count=True, help="Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE"
    )(command)
    command = optgroup.option("-q", "--quiet", is_flag=True, help="Suppress all console output")(command)
    command = optgroup.option(
        "--format",
        "output_format",
        default="human",
        show_default=True,
        help="Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided, fields use standard python templating like 'name: {agent.name}' See below for available fields.",
    )(command)
    # Start the "Common" option group - applied last since decorators run in reverse order
    command = optgroup.group(COMMON_OPTIONS_GROUP_NAME)(command)

    return command


def setup_command_context(
    ctx: click.Context,
    command_name: str,
    command_class: type[TCommandOptions],
    is_format_template_supported: bool = False,
    strict: bool | None = None,
    silent_unknown_fields: bool = False,
) -> tuple[MngrContext, OutputOptions, TCommandOptions]:
    """Set up config and logging for a command.

    This is the single entry point for command setup. Call this at the top of
    each command to load config, parse output options, apply config defaults,
    set up logging, and load plugin backends.

    Set is_format_template_supported=True for commands that handle
    output_opts.format_template.

    Set ``silent_unknown_fields=True`` to suppress warnings about unknown
    config fields and unknown provider backends (used by ``mngr plugin add``,
    where the config is expected to reference plugins that are not yet
    installed). Only takes effect when ``strict=False``.

    The resolved LoggingConfig (with CLI overrides applied) is stored on the
    click context at ctx.meta["logging_config"] for callers that need logging
    levels (e.g., LoggingSuppressor).

    Plugin-registered CLI option values are stored in ctx.meta["plugin_cli_params"]
    as a dict, accessible by plugins via their hooks.
    """
    # Separate plugin-registered params from known command class fields
    known_params, plugin_params = _split_known_and_plugin_params(ctx.params, command_class)

    # First parse options from CLI args to extract common parameters
    initial_opts = command_class(**known_params)

    # Create a top-level ConcurrencyGroup for process management
    cg = ConcurrencyGroup(name=f"mngr-{command_name}")
    cg.__enter__()
    # We explicitly pass None to __exit__ so that Click exceptions (e.g. UsageError) don't get
    # wrapped in ConcurrencyExceptionGroup, which would break Click's error handling.
    ctx.call_on_close(lambda: cg.__exit__(None, None, None))

    # Resolve strict here so the same policy applies to both load_config (which
    # validates section field names) and apply_config_defaults below (which
    # validates command parameter names).
    if strict is None:
        strict = resolve_strict_from_env()

    # Load config (is_interactive will be resolved below). The ``mngr config`` command is
    # exempt from the settings-narrowing guard: it must be able to load a config that would
    # otherwise narrow in order to *edit* it (otherwise `mngr config set`/`unset` -- the way
    # to fix a narrowing config -- would themselves fail with the narrowing error).
    pm = ctx.obj
    mngr_ctx = load_config(
        pm,
        cg,
        enabled_plugins=initial_opts.plugin,
        disabled_plugins=initial_opts.disable_plugin,
        is_interactive=False,
        strict=strict,
        silent_unknown_fields=silent_unknown_fields,
        enforce_narrowing_guard=command_name != "config",
    )

    # Resolve is_interactive from all sources.
    # Precedence: --headless CLI flag > config/env headless > TTY auto-detect
    if initial_opts.headless or mngr_ctx.config.headless:
        is_interactive = False
    else:
        try:
            is_interactive = sys.stdout.isatty()
        except (ValueError, AttributeError):
            # Handle cases where stdout is uninitialized (e.g., xdist workers)
            is_interactive = False

    # Update MngrContext with the resolved is_interactive and safe mode
    mngr_ctx = mngr_ctx.model_copy_update(
        to_update(mngr_ctx.field_ref().is_interactive, is_interactive),
        to_update(mngr_ctx.field_ref().is_full_discovery, initial_opts.safe),
    )

    # Capture the originally-loaded config before any --setting is applied. The
    # end-of-pipeline re-application (which folds in create-template-contributed
    # settings) runs against this pristine base so it never double-applies an
    # ``__extend`` operator.
    base_config = mngr_ctx.config

    # Apply --setting overrides to config (right before CLI defaults are applied).
    # This uses the CLI -S flags only; config-default and create-template
    # ``setting`` entries are folded in later via the end-of-pipeline pass so they
    # can affect command-default resolution and template resolution first.
    if initial_opts.setting:
        updated_config = apply_settings_to_config(
            mngr_ctx.config,
            initial_opts.setting,
            mngr_ctx.config.disabled_plugins,
        )
        mngr_ctx = mngr_ctx.model_copy_update(
            to_update(mngr_ctx.field_ref().config, updated_config),
        )

    # Pipeline order is: config_defaults -> templates -> CLI extension.
    #
    # 1. apply_config_defaults substitutes the config value as the BASE for each
    #    tuple/list param (regardless of CLI source) so templates can apply
    #    assign-by-default + ``__extend`` against a clean config base. Scalar
    #    CLI-source params keep their CLI value through this step.
    updated_params = apply_config_defaults(ctx, mngr_ctx.config, command_name, strict=strict)

    # 2. Stash CLI tuple/list values from the raw click context for restoration
    #    after templates resolve. (Reads ctx.params, not updated_params, because
    #    updated_params already has the config base for those fields.)
    cli_list_values = save_cli_list_values_for_restoration(ctx)

    # 3. Apply create templates (create-only) against the config base.
    if command_name == "create":
        updated_params = apply_create_template(ctx, updated_params, mngr_ctx.config)

    # 4. Append CLI tuple/list values to the template result so CLI ends up at
    #    the tail of the merged list (config_base + template_result + CLI).
    updated_params = restore_cli_list_values(updated_params, cli_list_values)

    # 5. Fold create-template (and config-default) ``setting`` entries into the
    #    config. apply_create_template appends them to ``updated_params["setting"]``,
    #    but the only --setting application above used the CLI flags, so without
    #    this pass template-provided settings would never reach the resolved
    #    config (e.g. providers.<name>.docker_runtime). Re-apply against the
    #    pristine base config so CLI -S still wins and no ``__extend`` is doubled.
    if command_name == "create":
        mngr_ctx = _apply_template_contributed_settings(
            mngr_ctx,
            base_config=base_config,
            combined_settings=updated_params.get("setting", ()),
            cli_settings=initial_opts.setting,
            template_names=updated_params.get("template", ()),
        )

    # Block plugins that were disabled via command defaults or create templates
    # (e.g. disable_plugin from [commands.create] in settings.toml). load_config
    # only blocks plugins from CLI args and [plugins] config sections; command
    # defaults are applied later and need a second blocking pass.
    updated_disable_plugin = updated_params.get("disable_plugin", ())
    if updated_disable_plugin:
        block_disabled_plugins(pm, frozenset(updated_disable_plugin))

    # Allow plugins to override command options before creating the options object
    _apply_plugin_option_overrides(pm, command_name, command_class, updated_params)

    # Re-separate after config defaults and plugin overrides may have changed things
    known_updated_params, updated_plugin_params = _split_known_and_plugin_params(updated_params, command_class)

    # Store plugin CLI params so plugins can access their values via hooks
    ctx.meta["plugin_cli_params"] = updated_plugin_params

    # Re-create options with config defaults applied
    opts = command_class(**known_updated_params)

    # Parse output options and resolve logging config with CLI overrides applied.
    output_opts, resolved_logging_config = parse_output_options(
        output_format=opts.output_format,
        quiet=opts.quiet,
        verbose=opts.verbose,
        log_file=opts.log_file,
        log_commands=opts.log_commands,
        config=mngr_ctx.config,
    )

    # Reject format templates on commands that don't support them
    if output_opts.format_template is not None and not is_format_template_supported:
        raise click.UsageError(
            f"Format template strings are not supported by the '{command_name}' command. "
            "Use --format human, --format json, or --format jsonl."
        )

    # Store resolved logging config on the click context for callers that need it
    ctx.meta["logging_config"] = resolved_logging_config

    # Set up logging
    setup_logging(resolved_logging_config, default_host_dir=mngr_ctx.config.default_host_dir, command=command_name)

    # Enter a log span for the command lifetime
    span = log_span("Started {} command", command_name)
    ctx.with_resource(span)

    # Register interactive state and error reporting state on the group context
    # so AliasAwareGroup.invoke() can check them when catching exceptions
    if ctx.parent is not None:
        ctx.parent.meta["is_interactive"] = is_interactive
        # Expose the resolved output format on the group context so
        # AliasAwareGroup.invoke() can emit a structured JSONL error event
        # (with the exception's class name) when a command fails -- letting
        # subprocess callers detect the error *type* without parsing text.
        ctx.parent.meta["output_format"] = output_opts.output_format
        if mngr_ctx.config.is_error_reporting_enabled and is_interactive:
            ctx.parent.meta["is_error_reporting_enabled"] = True

    # Run pre-command scripts if configured for this command
    _run_pre_command_scripts(mngr_ctx.config, command_name, cg, cwd=mngr_ctx.project_root)

    # Store command metadata for lifecycle hooks (on_after_command, on_error)
    if ctx.parent is not None:
        ctx.parent.meta["hook_command_name"] = command_name
        ctx.parent.meta["hook_command_params"] = updated_params

    # Call on_before_command hook (plugins can raise to abort)
    pm.hook.on_before_command(command_name=command_name, command_params=updated_params)

    return mngr_ctx, output_opts, opts


def parse_output_options(
    output_format: str,
    quiet: bool,
    verbose: int,
    log_file: str | None,
    log_commands: bool | None,
    config: MngrConfig,
) -> tuple[OutputOptions, LoggingConfig]:
    """Parse output-related CLI options. CLI flags can override config values.

    Returns a tuple of (OutputOptions, resolved LoggingConfig). The resolved
    LoggingConfig contains the TOML defaults with CLI overrides applied.

    If output_format is a built-in format name (human, json, jsonl), it is parsed
    as an OutputFormat enum. Otherwise it is treated as a format template string:
    the output_format is set to HUMAN and the template is stored in format_template
    (with shell escape sequences like \\t and \\n interpreted).
    """
    # Detect whether the format string is a built-in format or a template
    parsed_output_format: OutputFormat
    format_template: str | None = None

    if output_format.lower() in _BUILTIN_FORMAT_NAMES:
        parsed_output_format = OutputFormat(output_format.upper())
    else:
        # Validate template syntax early
        try:
            list(string.Formatter().parse(output_format))
        except (ValueError, KeyError) as e:
            raise click.UsageError(f"Invalid format template: {e}") from None
        # Interpret shell escape sequences (\t -> tab, \n -> newline, etc.)
        format_template = _process_template_escapes(output_format)
        parsed_output_format = OutputFormat.HUMAN

    # Determine console level based on quiet and verbose flags
    if quiet:
        console_level = LogLevel.NONE
    elif verbose >= 2:
        console_level = LogLevel.TRACE
    elif verbose == 1:
        console_level = LogLevel.DEBUG
    else:
        console_level = config.logging.console_level

    # Parse log file path
    log_file_path = Path(log_file) if log_file else None

    # Use CLI overrides if provided, otherwise use config
    is_log_commands = log_commands if log_commands is not None else config.logging.is_logging_commands

    # Build the resolved logging config with CLI overrides applied to TOML defaults
    resolved_logging_config = LoggingConfig(
        file_level=config.logging.file_level,
        log_dir=config.logging.log_dir,
        max_log_size_mb=config.logging.max_log_size_mb,
        console_level=console_level,
        log_file_path=log_file_path,
        is_logging_commands=is_log_commands,
        is_logging_command_output=config.logging.is_logging_command_output,
        is_logging_env_vars=config.logging.is_logging_env_vars,
        enable_paramiko_logging=config.logging.enable_paramiko_logging,
    )

    output_opts = OutputOptions(
        output_format=parsed_output_format,
        format_template=format_template,
        is_quiet=quiet,
    )

    return output_opts, resolved_logging_config


@pure
def _process_template_escapes(template: str) -> str:
    """Interpret common backslash escape sequences in a template string.

    The shell passes \\t, \\n, etc. as literal characters. This function converts
    them to actual tab, newline, etc. -- matching the behavior of tools like awk
    and printf. Uses a single-pass scanner to correctly handle sequences like
    \\\\t (literal backslash + t) without re-processing.
    """
    escape_map = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}
    result: list[str] = []
    idx = 0
    while idx < len(template):
        char = template[idx]
        if char == "\\" and idx + 1 < len(template):
            next_char = template[idx + 1]
            if next_char in escape_map:
                result.append(escape_map[next_char])
                idx += 2
                continue
        result.append(char)
        idx += 1
    return "".join(result)


@pure
def apply_settings_to_config(
    config: MngrConfig,
    settings: Sequence[str],
    disabled_plugins: frozenset[str],
) -> MngrConfig:
    """Apply --setting KEY=VALUE overrides to a loaded config.

    Parses each setting string into a raw dict, resolves any ``__extend``
    suffixes against ``config`` via the shared key resolver, parses the
    resolved dict through ``parse_config``, and merges with ``config``. This
    gives --setting the same semantics as config file values but at a higher
    precedence, with assign-vs-extend behavior unified across TOML, env vars,
    --setting, and ``mngr config``.
    """
    if not settings:
        return config

    raw: dict[str, Any] = {}
    for setting_str in settings:
        if "=" not in setting_str:
            raise UserInputError(
                f"Invalid --setting format: '{setting_str}'. "
                "Expected KEY=VALUE (e.g., '--setting commands.create.connect=false')"
            )
        key_path, value_str = setting_str.split("=", 1)
        key_path = key_path.strip()
        if not key_path:
            raise UserInputError("Invalid --setting: key cannot be empty")
        parsed_value = parse_scalar_value(value_str)
        set_at_path(raw, key_path.split("."), parsed_value)

    resolved = resolve_extends(config, raw)
    settings_config = parse_config(resolved, disabled_plugins=disabled_plugins, strict=True)
    # The settings-narrowing guard runs while the settings files and env vars are
    # loaded, before --setting is applied, so its opt-in flag would be silently
    # ineffective there. A non-None value here means the user tried to set it via
    # --setting, so reject it with a pointer to where it actually works rather
    # than accepting it as a no-op.
    if settings_config.allow_settings_key_assignment_narrowing is not None:
        raise UserInputError(
            "`allow_settings_key_assignment_narrowing` cannot be set with --setting. It "
            "controls the settings-narrowing guard, which runs while the settings files and "
            "env vars are loaded (before --setting is applied). Set "
            "`allow_settings_key_assignment_narrowing = true` in a settings.toml, or set "
            "MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING=true."
        )
    # Apply the same narrowing guard used by the config-file merge path so
    # ``--setting`` cannot silently drop entries from the merged config either.
    # Honor the existing setting on ``config``, since ``--setting`` runs after
    # config-file loading, so the resolved value is already known here.
    merged, violations = config.merge_with(settings_config)
    if violations and not config.allow_settings_key_assignment_narrowing:
        raise _build_setting_narrowing_error(violations)
    return merged


# Top-level setting key segments whose effect is consumed earlier in
# setup_command_context than the point where create-template ``setting`` entries
# are folded into the config. A template (or command-default) ``setting``
# targeting these can never take effect, so we reject it rather than dropping it
# silently. Direct CLI ``-S`` for these keys is unaffected -- it is applied
# before those phases run.
_INEFFECTIVE_TEMPLATE_SETTING_PREFIXES: frozenset[str] = frozenset({"commands", "create_templates"})


def _apply_template_contributed_settings(
    mngr_ctx: MngrContext,
    *,
    base_config: MngrConfig,
    combined_settings: Sequence[str],
    cli_settings: Sequence[str],
    template_names: Sequence[str],
) -> MngrContext:
    """Re-apply the post-template ``setting`` list to the config and return the updated context.

    After the create pipeline runs, ``combined_settings`` is
    ``config_default_settings + template_settings + cli_settings`` (the CLI flags
    are appended last by ``restore_cli_list_values``). Only the CLI flags were
    already merged into the config; the config-default and template-contributed
    entries were not. Re-apply the whole list against the pristine ``base_config``
    so those entries reach the resolved config, preserving "CLI wins" precedence
    (CLI entries are last, and later same-key entries win) and avoiding
    double-applying ``__extend`` operators.
    """
    combined = tuple(combined_settings)
    cli = tuple(cli_settings)

    # The CLI flags are the tail of the combined list; everything before them is
    # contributed by config defaults or create templates and has not yet been
    # applied to the config.
    non_cli_count = len(combined) - len(cli)
    if non_cli_count <= 0:
        return mngr_ctx
    assert combined[non_cli_count:] == cli, (
        "Expected the CLI --setting flags to be the tail of the resolved setting list; "
        f"got combined={combined!r}, cli={cli!r}"
    )

    non_cli_settings = combined[:non_cli_count]
    _reject_ineffective_template_setting_keys(non_cli_settings, template_names)

    final_config = apply_settings_to_config(base_config, combined, base_config.disabled_plugins)
    return mngr_ctx.model_copy_update(
        to_update(mngr_ctx.field_ref().config, final_config),
    )


def _reject_ineffective_template_setting_keys(settings: Sequence[str], template_names: Sequence[str]) -> None:
    """Raise ``ConfigParseError`` if a non-CLI ``setting`` entry targets a key that cannot take effect.

    ``commands.*`` (command defaults) and ``create_templates.*`` are resolved
    earlier in setup_command_context than the point where create-template
    ``setting`` entries are folded into the config, so such entries would be
    silently ignored.
    """
    for setting_str in settings:
        if "=" not in setting_str:
            continue
        key_path = setting_str.split("=", 1)[0].strip()
        if not key_path:
            continue
        # Normalize hyphens to underscores so ``create-templates.*`` is caught
        # the same way ``create_templates.*`` is (config key parsing treats them
        # as equivalent).
        first_segment = key_path.split(".", 1)[0].replace("-", "_")
        if first_segment in _INEFFECTIVE_TEMPLATE_SETTING_PREFIXES:
            template_hint = f" (from create template(s): {', '.join(template_names)})" if template_names else ""
            raise ConfigParseError(
                f"A create-template (or command-default) `setting` entry targets `{key_path}`{template_hint}, "
                f"which cannot take effect: `commands.*` and `create_templates.*` are resolved before "
                f"template settings are applied to the config, so the value would be silently ignored.\n"
                f"Set this directly under the matching `[{first_segment}.*]` config section instead, or pass "
                f"it as a direct CLI `-S {key_path}=...` (which is applied early enough to take effect)."
            )


def _build_setting_narrowing_error(violations: Sequence[str]) -> ConfigParseError:
    """Construct the user-facing error for ``--setting`` narrowing assignments.

    Mirrors the loader's message but attributes the violations to ``--setting``
    and reminds users that they can opt in either by setting the safety field
    to True or by switching the specific key to ``__extend``.
    """
    detail_lines = [f"  --setting: {key}" for key in violations]
    return ConfigParseError(
        "Settings narrowing detected: a --setting override would assign over a non-empty "
        "list/tuple/dict/set value from the merged config, silently dropping the earlier "
        "entries.\n" + "\n".join(detail_lines) + "\n"
        "To opt into this assign-by-default behavior (and silence this error), set "
        "`allow_settings_key_assignment_narrowing = true` in your settings.toml.\n"
        "To keep the additive behavior for a specific key, switch to the `__extend` suffix on "
        "the --setting key (e.g. `--setting commands.create.env__extend='[\"X=5\"]'`)."
    )


def apply_config_defaults(
    ctx: click.Context,
    config: MngrConfig,
    command_name: str,
    *,
    strict: bool = True,
) -> dict[str, Any]:
    """Apply config defaults and prepare the "config base" for the template phase.

    For tuple/list params: the config value (if any) is substituted in
    regardless of parameter source, becoming the BASE that templates apply
    against and that the CLI value is appended to at the end of the pipeline.
    Any CLI-source tuple/list param WITHOUT a config entry is reset to ``()``
    here for the same reason: if we left the CLI value in, the final
    ``restore_cli_list_values`` step would double-append it. The original
    CLI tuple/list value is captured separately by
    ``save_cli_list_values_for_restoration`` so it can be restored later.

    For scalar params: only DEFAULT-source params are substituted with the
    config value. CLI-source scalars keep their CLI value (and templates skip
    them via their own source check), preserving the "CLI scalar wins" contract.

    Special handling: an empty string config value (``""``) for a tuple/list
    param clears the list to ``()`` -- this is how env vars like
    ``MNGR__COMMANDS__CREATE__ADD_COMMAND=`` can clear config defaults.

    When strict=True, raises ConfigParseError for unknown parameter names; when
    strict=False, logs a warning and skips them. Callers should resolve the
    policy from MNGR_ALLOW_UNKNOWN_CONFIG once and pass the result through.
    """
    command_defaults = config.commands.get(command_name)
    config_defaults: dict[str, Any] = command_defaults.defaults if command_defaults else {}

    updated_params = ctx.params.copy()

    # First pass: every param with a config entry gets its value processed.
    for param_name, config_value in config_defaults.items():
        if param_name not in ctx.params:
            msg = (
                f"Unknown parameter '{param_name}' in commands.{command_name} config. "
                f"Valid parameters: {sorted(ctx.params.keys())}"
            )
            if not strict:
                logger.warning(msg)
                continue
            raise ConfigParseError(msg)

        current_value = ctx.params[param_name]

        # Tuple/list params: config is the base regardless of CLI source.
        if isinstance(current_value, tuple):
            if config_value == "":
                updated_params[param_name] = ()
            elif isinstance(config_value, (list, tuple)):
                updated_params[param_name] = tuple(config_value)
            else:
                # Shape mismatch (config gave a non-aggregate for a tuple param);
                # pass it through so downstream validation can catch it.
                updated_params[param_name] = config_value
            continue

        # Scalar params: only DEFAULT-source values are overridden by config.
        if ctx.get_parameter_source(param_name) == ParameterSource.DEFAULT:
            updated_params[param_name] = config_value

    # Second pass: any CLI-source tuple/list param WITHOUT a config entry needs
    # the CLI value cleared so restore_cli_list_values doesn't double-append.
    # Click's "multiple=True" options default to (), which is the value we'd
    # have had if the user hadn't supplied --flag at all; reset to that.
    # Control tuples (see ``_NON_BOOKENDED_CLI_TUPLE_PARAMS``) are exempt --
    # those need their CLI value to remain visible to downstream consumers.
    for param_name, value in ctx.params.items():
        if not isinstance(value, tuple):
            continue
        if param_name in config_defaults:
            continue
        if param_name in _NON_BOOKENDED_CLI_TUPLE_PARAMS:
            continue
        if ctx.get_parameter_source(param_name) != ParameterSource.COMMANDLINE:
            continue
        updated_params[param_name] = ()

    return updated_params


# Tuple-typed CLI params that drive control flow (which templates to apply, etc.)
# rather than carrying data that gets merged with config defaults. The bookend
# pattern (apply_config_defaults's reset + save_cli_list_values_for_restoration +
# restore_cli_list_values) must skip them so the original CLI value stays visible
# to whatever consumer needs it.
_NON_BOOKENDED_CLI_TUPLE_PARAMS: frozenset[str] = frozenset({"template"})


def save_cli_list_values_for_restoration(ctx: click.Context) -> dict[str, tuple[Any, ...]]:
    """Capture the original CLI-supplied tuple/list values from ``ctx.params`` so
    ``restore_cli_list_values`` can re-apply them at the end of the pipeline.

    Reads from ``ctx.params`` (not the post-config-defaults params dict) because
    ``apply_config_defaults`` substitutes config values into CLI-source tuple/list
    params -- the config value becomes the "base" the template phase operates on.
    The CLI value needs to come from the unmodified click context to survive.

    Pipeline order is therefore:

        apply_config_defaults       -> params hold config_base for tuple/list fields
        save_cli_list_values_...    -> stash CLI value separately
        apply_create_template       -> templates assign / __extend against config_base
        restore_cli_list_values     -> append stashed CLI values to template result

    Final value for a CLI-supplied tuple/list param is therefore
    ``config_base + template_resolution + CLI_value`` -- the CLI value reads as
    the user's final word at the tail of the list. Control params listed in
    ``_NON_BOOKENDED_CLI_TUPLE_PARAMS`` are excluded -- they need to flow
    through to consumers (e.g. ``apply_create_template``) without being
    rewritten.
    """
    return {
        param_name: tuple(value)
        for param_name, value in ctx.params.items()
        if isinstance(value, (list, tuple))
        and ctx.get_parameter_source(param_name) == ParameterSource.COMMANDLINE
        and param_name not in _NON_BOOKENDED_CLI_TUPLE_PARAMS
    }


def restore_cli_list_values(params: dict[str, Any], cli_list_values: dict[str, tuple[Any, ...]]) -> dict[str, Any]:
    """Append the CLI list values previously captured by
    ``save_cli_list_values_for_restoration`` to whatever the template phase
    produced for the corresponding params.

    Final ordering for a tuple/list parameter is::

        config_defaults  +  template_resolution_result  +  CLI_supplied_values

    so the CLI flag always reads as "what the user explicitly added on top."
    """
    updated_params = params.copy()
    for param_name, cli_value in cli_list_values.items():
        current = updated_params.get(param_name, ())
        if isinstance(current, (list, tuple)):
            updated_params[param_name] = tuple(current) + cli_value
        else:
            updated_params[param_name] = cli_value
    return updated_params


def apply_create_template(
    ctx: click.Context,
    params: dict[str, Any],
    config: MngrConfig,
) -> dict[str, Any]:
    """Apply create templates to parameters if any are specified.

    Templates are named presets of create command arguments that can be applied
    using --template <name>. Multiple templates can be specified and are applied
    in order, stacking their values. Templates run AFTER ``apply_config_defaults``
    (so the "base" for any option is the post-config-defaults value -- i.e. the
    config-supplied value for tuple/list params, or ``()`` when no config entry
    exists) and BEFORE ``restore_cli_list_values`` (the original CLI tuple/list
    value was captured separately by ``save_cli_list_values_for_restoration``
    and is appended to the template result at the end of the pipeline).

    Merge semantics (now aligned with the rest of the settings/merge_with rules):
    - Scalar params from a template: assign-by-default; the latest template that
      writes the param wins, and CLI-supplied scalars always take precedence
      (templates skip them via the parameter-source check).
    - Aggregate params from a template (list / tuple / dict / set): assign-by-default.
      A template assigning over a non-empty base that would drop entries is a
      narrowing assignment and raises ``ConfigParseError`` unless
      ``config.allow_settings_key_assignment_narrowing`` is ``True``. To opt into
      additive behavior for a specific key, write ``key__extend = [...]`` in the
      template definition -- the same operator suffix recognised in TOML,
      ``--setting``, and env vars.

    This function should only be called for the 'create' command.
    """
    template_names = params.get("template", ())
    if not template_names:
        return params

    # Start with existing params
    updated_params = params.copy()

    # Apply each template in order
    for template_name in template_names:
        try:
            template_key = CreateTemplateName(template_name)
        except ParseSpecError as e:
            raise UserInputError(f"Invalid template name: {e}") from e

        if template_key not in config.create_templates:
            available = list(config.create_templates.keys())
            if available:
                raise UserInputError(
                    f"Template '{template_name}' not found. Available templates: {', '.join(str(t) for t in available)}"
                )
            else:
                raise UserInputError(
                    f"Template '{template_name}' not found. No templates are configured. "
                    "Add templates to your settings.toml under [create_templates.<name>]"
                )

        template = config.create_templates[template_key]

        for raw_key, template_value in template.options.items():
            if template_value is None:
                continue

            # Detect the ``__extend`` operator suffix; bare keys are assign-by-default,
            # ``<key>__extend`` opts into additive behavior for the targeted key.
            is_extend = is_extend_key(raw_key)
            param_name = bare_key(raw_key) if is_extend else raw_key
            if param_name not in params:
                continue

            existing_value = updated_params[param_name]

            if is_extend:
                updated_params[param_name] = _apply_template_extend(
                    existing_value,
                    template_value,
                    template_name=template_name,
                    param_name=param_name,
                )
                continue

            # Bare assign on a scalar field: CLI wins; otherwise template overrides.
            if not isinstance(template_value, (list, tuple, dict, set, frozenset)):
                if ctx.get_parameter_source(param_name) == ParameterSource.DEFAULT:
                    updated_params[param_name] = template_value
                # CLI-supplied scalar: CLI wins (no change)
                continue

            # Bare assign on an aggregate field: check the narrowing guard.
            if would_assignment_narrow(existing_value, template_value):
                if not config.allow_settings_key_assignment_narrowing:
                    raise ConfigParseError(_build_template_narrowing_message(template_name, param_name))

            # Coerce list -> tuple when the existing value is a tuple so downstream
            # CLI code keeps seeing the canonical tuple shape it expects.
            if isinstance(existing_value, tuple) and isinstance(template_value, list):
                updated_params[param_name] = tuple(template_value)
            else:
                updated_params[param_name] = template_value

    return updated_params


def _apply_template_extend(
    existing_value: Any,
    extend_value: Any,
    *,
    template_name: str,
    param_name: str,
) -> Any:
    """Apply a single template's ``<key>__extend = ...`` against the existing
    parameter value, delegating to the shared ``extend_plain_value`` algebra.

    Operates against in-flight click param values rather than parsed pydantic
    models; ``extend_plain_value`` preserves tuple-ness when the base is a tuple, which
    keeps the click-native tuple shape downstream code expects. The dict branch is
    recursive (a nested ``key__extend`` extends rather than replaces). Re-raise the
    overlay's ``OverlayError`` as a ``ConfigParseError`` so the template-specific
    ``field_path`` still appears in the message.
    """
    field_path = f"create_templates.{template_name}.{param_name}__extend"
    try:
        return extend_plain_value(existing_value, extend_value, field_path)
    except OverlayError as e:
        raise ConfigParseError(str(e)) from e


def _build_template_narrowing_message(template_name: str, param_name: str) -> str:
    """Construct the user-facing error for a template assign-by-default that
    would silently drop entries from the existing parameter value.

    Same shape as the settings-layer narrowing error so users see consistent
    advice across config files, env vars, ``--setting``, and templates.
    """
    return (
        f"Settings narrowing detected: create_templates.{template_name}.{param_name} would "
        f"assign over a non-empty list/tuple/dict/set value from the merged config (or an "
        f"earlier template in the stack), silently dropping the earlier entries.\n"
        f"To opt into this assign-by-default behavior (and silence this error), set "
        f"`allow_settings_key_assignment_narrowing = true` in your settings.toml.\n"
        f"To keep the additive behavior for this specific key, switch the template entry to "
        f"`{param_name}__extend = [...]`."
    )


def is_param_explicit(ctx: click.Context, param_name: str) -> bool:
    """Check whether a CLI parameter was explicitly set on the command line."""
    return ctx.get_parameter_source(param_name) == ParameterSource.COMMANDLINE


@pure
def _split_known_and_plugin_params(
    params: dict[str, Any],
    command_class: type[CommonCliOptions],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split params into those known to the command class and extra plugin params."""
    known_fields = command_class.model_fields
    known_params: dict[str, Any] = {}
    plugin_params: dict[str, Any] = {}
    for k, v in params.items():
        (known_params if k in known_fields else plugin_params)[k] = v
    return known_params, plugin_params


def _apply_plugin_option_overrides(
    pm: pluggy.PluginManager,
    command_name: str,
    command_class: type,
    params: dict[str, Any],
) -> None:
    """Apply plugin overrides to command parameters.

    Calls the override_command_options hook for all registered plugins.
    Each plugin modifies the params dict in place.
    """
    pm.hook.override_command_options(
        command_name=command_name,
        command_class=command_class,
        params=params,
    )


def _run_single_script(script: str, cg: ConcurrencyGroup, cwd: Path | None) -> tuple[str, int, str, str]:
    """Run a single script and return (script, exit_code, stdout, stderr)."""
    try:
        result = cg.run_process_to_completion(
            ["sh", "-c", script],
            cwd=cwd,
        )
        return (script, result.returncode if result.returncode is not None else 0, result.stdout, result.stderr)
    except ProcessError as e:
        return (script, e.returncode if e.returncode is not None else -1, e.stdout, e.stderr)


def _run_pre_command_scripts(config: MngrConfig, command_name: str, cg: ConcurrencyGroup, cwd: Path | None) -> None:
    """Run pre-command scripts configured for this command.

    Scripts are run in parallel and all must succeed (exit code 0).
    When cwd is provided, scripts run with that as their working directory.
    Raises click.ClickException if any script fails.
    """
    scripts = config.pre_command_scripts.get(command_name)
    if not scripts:
        return

    # Run all scripts in parallel
    failures: list[tuple[str, int, str, str]] = []
    futures: list[Future[tuple[str, int, str, str]]] = []
    with mngr_executor(parent_cg=cg, name="pre_command_scripts", max_workers=32) as executor:
        for script in scripts:
            futures.append(executor.submit(_run_single_script, script, cg, cwd))
    for future in futures:
        script, exit_code, _stdout, stderr = future.result()
        if exit_code != 0:
            failures.append((script, exit_code, _stdout, stderr))

    if failures:
        error_lines = [f"Pre-command script(s) failed for '{command_name}':"]
        for script, exit_code, _stdout, stderr in failures:
            error_lines.append(f"  Script: {script}")
            error_lines.append(f"  Exit code: {exit_code}")
            if stderr.strip():
                error_lines.append(f"  Stderr: {stderr.strip()}")
        raise click.ClickException("\n".join(error_lines))


def create_group_title_option(group: OptionGroup) -> click.Option:
    """Create a hidden option that renders the group title in help output.

    This creates an option dynamically with a custom get_help_record method
    that delegates to the group for rendering the group header.
    """
    fake_name = f"--fake-{uuid.uuid4().hex}"

    option = click.Option(
        [fake_name],
        hidden=True,
        expose_value=False,
        help=group.help,
    )
    # Clear opts so this option doesn't appear in usage
    option.opts = []
    option.secondary_opts = []

    # Monkey-patch get_help_record to delegate to the group
    option.get_help_record = lambda ctx: group.get_help_record(ctx)  # ty: ignore[invalid-assignment]

    return option


def find_option_group(command: click.Command, group_name: str) -> OptionGroup | None:
    """Find an existing option group on a command by name."""
    for param in command.params:
        if isinstance(param, GroupedOption) and param.group.name == group_name:
            return param.group
    return None


def find_last_option_index_in_group(command: click.Command, group: OptionGroup) -> int:
    """Find the index of the last option in a group, or -1 if none found."""
    last_index = -1
    for i, param in enumerate(command.params):
        if isinstance(param, GroupedOption) and param.group is group:
            last_index = i
    return last_index
