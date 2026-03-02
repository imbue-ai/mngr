import string
import sys
import uuid
from collections.abc import Callable
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

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mng.config.data_types import CreateTemplateName
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.config.loader import load_config
from imbue.mng.errors import ParseSpecError
from imbue.mng.errors import UserInputError
from imbue.mng.primitives import LogLevel
from imbue.mng.primitives import OutputFormat
from imbue.mng.utils.logging import LoggingConfig
from imbue.mng.utils.logging import setup_logging

# The set of built-in format names (case-insensitive). Any --format value not
# matching one of these is treated as a format template string.
_BUILTIN_FORMAT_NAMES: frozenset[str] = frozenset(f.value.lower() for f in OutputFormat)

# Constant for the "Common" option group name used across all commands
COMMON_OPTIONS_GROUP_NAME = "Common"

TCommandOptions = TypeVar("TCommandOptions", bound="CommonCliOptions")
TDecorated = TypeVar("TDecorated", bound=Callable[..., Any])
TCommand = TypeVar("TCommand", bound=click.Command)


class CommonCliOptions(FrozenModel):
    """Base class for common CLI options shared across all commands.

    This captures the options added by the @add_common_options decorator.
    All command-specific option classes should inherit from this class.

    Note that this class VERY INTENTIONALLY DOES NOT use Field() decorators with descriptions, defaults, etc.
    For that information, see the @add_common_options decorator and its click.option() decorators.
    """

    output_format: str
    json_flag: bool = False
    jsonl_flag: bool = False
    quiet: bool
    verbose: int
    log_file: str | None
    log_commands: bool | None
    log_command_output: bool | None
    log_env_vars: bool | None
    project_context_path: str | None
    plugin: tuple[str, ...]
    disable_plugin: tuple[str, ...]


def add_common_options(command: TDecorated) -> TDecorated:
    """Decorator to add common options to a command.

    Adds the following options in the "Common" option group:
    - --format: Output format (human/json/jsonl, or a template string)
    - --json: Alias for --format json
    - --jsonl: Alias for --format jsonl
    - -q, --quiet: Suppress console output
    - -v, --verbose: Increase verbosity
    - --log-file: Override log file path
    - --log-commands: Log executed commands
    - --log-command-output: Log command output
    - --log-env-vars: Log environment variables
    - --context: Project context directory
    - --plugin: Enable plugins
    - --disable-plugin: Disable plugins
    """
    # Apply decorators in reverse order (bottom to top)
    # These are wrapped in the "Common" option group
    command = optgroup.option("--disable-plugin", multiple=True, help="Disable a plugin [repeatable]")(command)
    command = optgroup.option("--plugin", "--enable-plugin", multiple=True, help="Enable a plugin [repeatable]")(
        command
    )
    command = optgroup.option(
        "--context",
        "project_context_path",
        type=click.Path(exists=True),
        help="Project context directory (for build context and loading project-specific config) [default: local .git root]",
    )(command)
    command = optgroup.option(
        "--log-env-vars/--no-log-env-vars", default=None, help="Log environment variables (security risk)"
    )(command)
    command = optgroup.option(
        "--log-command-output/--no-log-command-output", default=None, help="Log stdout/stderr from commands"
    )(command)
    command = optgroup.option(
        "--log-commands/--no-log-commands", default=None, help="Log commands that were executed"
    )(command)
    command = optgroup.option(
        "--log-file",
        type=click.Path(),
        default=None,
        help="Path to log file (overrides default ~/.mng/logs/<timestamp>-<pid>.json)",
    )(command)
    command = optgroup.option(
        "-v", "--verbose", count=True, help="Increase verbosity (default: BUILD); -v for DEBUG, -vv for TRACE"
    )(command)
    command = optgroup.option("-q", "--quiet", is_flag=True, help="Suppress all console output")(command)
    command = optgroup.option(
        "--jsonl",
        "jsonl_flag",
        is_flag=True,
        default=False,
        help="Alias for --format jsonl",
    )(command)
    command = optgroup.option(
        "--json",
        "json_flag",
        is_flag=True,
        default=False,
        help="Alias for --format json",
    )(command)
    command = optgroup.option(
        "--format",
        "output_format",
        default="human",
        show_default=True,
        help="Output format (human, json, jsonl, FORMAT): Output format for results. When a template is provided [experimental], fields use standard python templating like 'name: {agent.name}' See below for available fields.",
    )(command)
    # Start the "Common" option group - applied last since decorators run in reverse order
    command = optgroup.group(COMMON_OPTIONS_GROUP_NAME)(command)

    return command


def setup_command_context(
    ctx: click.Context,
    command_name: str,
    command_class: type[TCommandOptions],
    is_format_template_supported: bool = False,
) -> tuple[MngContext, OutputOptions, TCommandOptions]:
    """Set up config and logging for a command.

    This is the single entry point for command setup. Call this at the top of
    each command to load config, parse output options, apply config defaults,
    set up logging, and load plugin backends.

    Set is_format_template_supported=True for commands that handle
    output_opts.format_template.

    The resolved LoggingConfig (with CLI overrides applied) is stored on the
    click context at ctx.meta["logging_config"] for callers that need logging
    levels (e.g., LoggingSuppressor).
    """
    # First parse options from CLI args to extract common parameters
    initial_opts = command_class(**ctx.params)

    # Create a top-level ConcurrencyGroup for process management
    cg = ConcurrencyGroup(name=f"mng-{command_name}")
    cg.__enter__()
    # We explicitly pass None to __exit__ so that Click exceptions (e.g. UsageError) don't get
    # wrapped in ConcurrencyExceptionGroup, which would break Click's error handling.
    ctx.call_on_close(lambda: cg.__exit__(None, None, None))

    # Load config
    context_dir = Path(initial_opts.project_context_path) if initial_opts.project_context_path else None
    pm = ctx.obj
    # Determine if we're running interactively (stdout is a TTY)
    try:
        is_interactive = sys.stdout.isatty()
    except (ValueError, AttributeError):
        # Handle cases where stdout is uninitialized (e.g., xdist workers)
        is_interactive = False
    mng_ctx = load_config(
        pm,
        cg,
        context_dir=context_dir,
        enabled_plugins=initial_opts.plugin,
        disabled_plugins=initial_opts.disable_plugin,
        is_interactive=is_interactive,
    )

    # Apply config defaults to parameters that came from defaults (not user-specified)
    updated_params = apply_config_defaults(ctx, mng_ctx.config, command_name)

    # Apply create template if this is the create command and a template was specified
    if command_name == "create":
        updated_params = apply_create_template(ctx, updated_params, mng_ctx.config)

    # Allow plugins to override command options before creating the options object
    _apply_plugin_option_overrides(pm, command_name, command_class, updated_params)

    # Re-create options with config defaults applied
    opts = command_class(**updated_params)

    # Resolve --json / --jsonl flags into output_format before parsing output options.
    effective_format = _resolve_format_flags(ctx, opts)

    # Parse output options and resolve logging config with CLI overrides applied.
    output_opts, resolved_logging_config = parse_output_options(
        output_format=effective_format,
        quiet=opts.quiet,
        verbose=opts.verbose,
        log_file=opts.log_file,
        log_commands=opts.log_commands,
        log_command_output=opts.log_command_output,
        log_env_vars=opts.log_env_vars,
        config=mng_ctx.config,
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
    setup_logging(resolved_logging_config, default_host_dir=mng_ctx.config.default_host_dir)

    # Enter a log span for the command lifetime
    span = log_span("Started {} command", command_name)
    ctx.with_resource(span)

    # Register error reporting state on the group context so AliasAwareGroup.invoke()
    # can check it when catching unexpected exceptions
    if ctx.parent is not None and mng_ctx.config.is_error_reporting_enabled and is_interactive:
        ctx.parent.meta["is_error_reporting_enabled"] = True

    # Run pre-command scripts if configured for this command
    _run_pre_command_scripts(mng_ctx.config, command_name, cg)

    # Store command metadata for lifecycle hooks (on_after_command, on_error)
    if ctx.parent is not None:
        ctx.parent.meta["hook_command_name"] = command_name
        ctx.parent.meta["hook_command_params"] = updated_params

    # Call on_before_command hook (plugins can raise to abort)
    pm.hook.on_before_command(command_name=command_name, command_params=updated_params)

    return mng_ctx, output_opts, opts


def _resolve_format_flags(ctx: click.Context, opts: CommonCliOptions) -> str:
    """Resolve --json / --jsonl convenience flags into a single format string.

    Validates mutual exclusivity: --json and --jsonl cannot be used together,
    and neither can be combined with an explicit --format value.
    """
    if opts.json_flag and opts.jsonl_flag:
        raise click.UsageError("--json and --jsonl are mutually exclusive")

    if opts.json_flag or opts.jsonl_flag:
        format_source = ctx.get_parameter_source("output_format")
        is_format_explicit = format_source is not None and format_source != ParameterSource.DEFAULT
        if is_format_explicit:
            flag_name = "--json" if opts.json_flag else "--jsonl"
            raise click.UsageError(f"{flag_name} is mutually exclusive with --format")
        return "json" if opts.json_flag else "jsonl"

    return opts.output_format


def parse_output_options(
    output_format: str,
    quiet: bool,
    verbose: int,
    log_file: str | None,
    log_commands: bool | None,
    log_command_output: bool | None,
    log_env_vars: bool | None,
    config: MngConfig,
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

    is_log_command_output = (
        log_command_output if log_command_output is not None else config.logging.is_logging_command_output
    )

    is_log_env_vars = log_env_vars if log_env_vars is not None else config.logging.is_logging_env_vars

    # Build the resolved logging config with CLI overrides applied to TOML defaults
    resolved_logging_config = LoggingConfig(
        file_level=config.logging.file_level,
        log_dir=config.logging.log_dir,
        max_log_files=config.logging.max_log_files,
        max_log_size_mb=config.logging.max_log_size_mb,
        console_level=console_level,
        log_level=config.logging.log_level,
        log_file_path=log_file_path,
        is_logging_commands=is_log_commands,
        is_logging_command_output=is_log_command_output,
        is_logging_env_vars=is_log_env_vars,
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


def apply_config_defaults(ctx: click.Context, config: MngConfig, command_name: str) -> dict[str, Any]:
    """Apply config defaults to parameters that were not explicitly set by the user.

    Uses ctx.get_parameter_source() to detect which parameters came from defaults.
    Only overrides parameters that came from DEFAULT source, not COMMANDLINE or ENVIRONMENT.

    Special handling for tuple/list parameters:
    - An empty string value ("") clears the list (sets it to an empty tuple)
    - This allows env vars like MNG_COMMANDS_CREATE_ADD_COMMAND= to clear config defaults
    """
    # Get command defaults from config
    command_defaults = config.commands.get(command_name)
    if not command_defaults:
        # No config defaults for this command, return params as-is
        return ctx.params.copy()

    # Start with the existing params
    updated_params = ctx.params.copy()

    # For each parameter, check if it came from a default and if config has an override
    for param_name, config_value in command_defaults.defaults.items():
        # Check if this parameter exists in the context
        if param_name not in ctx.params:
            continue

        # Check the source of the parameter value
        source = ctx.get_parameter_source(param_name)

        # Only override if the value came from the default
        if source == ParameterSource.DEFAULT:
            # Handle empty string for tuple/list parameters (clears the list)
            current_value = ctx.params[param_name]
            if isinstance(current_value, tuple) and config_value == "":
                updated_params[param_name] = ()
            else:
                updated_params[param_name] = config_value

    return updated_params


def apply_create_template(
    ctx: click.Context,
    params: dict[str, Any],
    config: MngConfig,
) -> dict[str, Any]:
    """Apply create templates to parameters if any are specified.

    Templates are named presets of create command arguments that can be applied
    using --template <name>. Multiple templates can be specified and are applied
    in order, stacking their values. Template values act as defaults - they only
    override parameters that came from DEFAULT source, not user-specified values.

    When multiple templates are specified, later templates override earlier ones
    for the same parameter.

    CLI arguments always take precedence over template values.

    This function should only be called for the 'create' command.
    """
    template_names = params.get("template", ())
    if not template_names:
        return params

    # Start with existing params
    updated_params = params.copy()

    # Apply each template in order (later templates override earlier ones)
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

        # Apply template options only for parameters that came from defaults (not CLI)
        for param_name, template_value in template.options.items():
            if template_value is None:
                continue
            if param_name not in params:
                continue
            source = ctx.get_parameter_source(param_name)
            if source == ParameterSource.DEFAULT:
                updated_params[param_name] = template_value

    return updated_params


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


def _run_single_script(script: str, cg: ConcurrencyGroup) -> tuple[str, int, str, str]:
    """Run a single script and return (script, exit_code, stdout, stderr)."""
    try:
        result = cg.run_process_to_completion(
            ["sh", "-c", script],
        )
        return (script, result.returncode if result.returncode is not None else 0, result.stdout, result.stderr)
    except ProcessError as e:
        return (script, e.returncode if e.returncode is not None else -1, e.stdout, e.stderr)


def _run_pre_command_scripts(config: MngConfig, command_name: str, cg: ConcurrencyGroup) -> None:
    """Run pre-command scripts configured for this command.

    Scripts are run in parallel and all must succeed (exit code 0).
    Raises click.ClickException if any script fails.
    """
    scripts = config.pre_command_scripts.get(command_name)
    if not scripts:
        return

    # Run all scripts in parallel
    failures: list[tuple[str, int, str, str]] = []
    futures: list[Future[tuple[str, int, str, str]]] = []
    with ConcurrencyGroupExecutor(parent_cg=cg, name="pre_command_scripts", max_workers=32) as executor:
        for script in scripts:
            futures.append(executor.submit(_run_single_script, script, cg))
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
