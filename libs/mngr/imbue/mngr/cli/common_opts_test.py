"""Tests for common_opts module."""

from typing import Any

import click
import pluggy
import pytest
from click.core import ParameterSource
from click.testing import CliRunner

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.cli.common_opts import _process_template_escapes
from imbue.mngr.cli.common_opts import _run_pre_command_scripts
from imbue.mngr.cli.common_opts import _run_single_script
from imbue.mngr.cli.common_opts import _split_known_and_plugin_params
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import apply_config_defaults
from imbue.mngr.cli.common_opts import apply_create_template
from imbue.mngr.cli.common_opts import parse_output_options
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import CreateTemplate
from imbue.mngr.config.data_types import CreateTemplateName
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import OutputFormat


def _make_click_context(
    params: dict[str, Any],
    # Maps param names to their source; defaults to ParameterSource.DEFAULT for all params
    source_by_param_name: dict[str, ParameterSource] | None = None,
) -> click.Context:
    """Create a real click.Context with the given params and parameter sources."""
    ctx = click.Context(click.Command("test"))
    ctx.params = params
    for param_name in params:
        source = (source_by_param_name or {}).get(param_name, ParameterSource.DEFAULT)
        ctx.set_parameter_source(param_name, source)
    return ctx


def test_run_single_script_success(cg: ConcurrencyGroup) -> None:
    """_run_single_script should return exit code 0 for successful command."""
    script, exit_code, stdout, stderr = _run_single_script("echo hello", cg)
    assert script == "echo hello"
    assert exit_code == 0
    assert "hello" in stdout
    assert stderr == ""


def test_run_single_script_failure(cg: ConcurrencyGroup) -> None:
    """_run_single_script should return non-zero exit code for failed command."""
    script, exit_code, stdout, stderr = _run_single_script("exit 1", cg)
    assert script == "exit 1"
    assert exit_code == 1


def test_run_single_script_captures_stderr(cg: ConcurrencyGroup) -> None:
    """_run_single_script should capture stderr from failed command."""
    script, exit_code, stdout, stderr = _run_single_script("echo error >&2 && exit 1", cg)
    assert exit_code == 1
    assert "error" in stderr


def test_run_pre_command_scripts_no_scripts(mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should do nothing if no scripts configured."""
    config = MngrConfig(prefix=mngr_test_prefix, pre_command_scripts={})
    # Should not raise
    _run_pre_command_scripts(config, "create", cg)


def test_run_pre_command_scripts_no_scripts_for_command(mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should do nothing if no scripts for this command."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"other_command": ["echo hello"]},
    )
    # Should not raise
    _run_pre_command_scripts(config, "create", cg)


def test_run_pre_command_scripts_success(mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should succeed when all scripts pass."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"create": ["echo first", "echo second"]},
    )
    # Should not raise
    _run_pre_command_scripts(config, "create", cg)


def test_run_pre_command_scripts_single_failure(mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should raise ClickException when a script fails."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"create": ["exit 1"]},
    )
    with pytest.raises(click.ClickException) as exc_info:
        _run_pre_command_scripts(config, "create", cg)
    assert "Pre-command script(s) failed" in str(exc_info.value)
    assert "exit 1" in str(exc_info.value)
    assert "Exit code: 1" in str(exc_info.value)


def test_run_pre_command_scripts_multiple_failures(mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should report all failures."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"create": ["exit 1", "exit 2"]},
    )
    with pytest.raises(click.ClickException) as exc_info:
        _run_pre_command_scripts(config, "create", cg)
    error_message = str(exc_info.value)
    assert "Pre-command script(s) failed" in error_message
    # Both failures should be reported
    assert "exit 1" in error_message or "exit 2" in error_message


def test_run_pre_command_scripts_partial_failure(mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should fail even if only one script fails."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"create": ["echo success", "exit 42"]},
    )
    with pytest.raises(click.ClickException) as exc_info:
        _run_pre_command_scripts(config, "create", cg)
    assert "Exit code: 42" in str(exc_info.value)


def test_run_pre_command_scripts_includes_stderr_in_error(mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should include stderr in error message."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"create": ["echo 'my error message' >&2 && exit 1"]},
    )
    with pytest.raises(click.ClickException) as exc_info:
        _run_pre_command_scripts(config, "create", cg)
    assert "my error message" in str(exc_info.value)


def test_apply_config_defaults_empty_string_clears_tuple_param(mngr_test_prefix: str) -> None:
    """apply_config_defaults should convert empty string to empty tuple for tuple params."""
    ctx = _make_click_context(
        params={"extra_window": ("default_cmd",), "other_param": "value"},
    )

    # Create config with empty string for the tuple param (simulating env var override)
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"extra_window": ""})},
    )

    result = apply_config_defaults(ctx, config, "create")

    # Empty string should be converted to empty tuple for tuple params
    assert result["extra_window"] == ()


def test_apply_config_defaults_non_empty_string_replaces_tuple_param(mngr_test_prefix: str) -> None:
    """apply_config_defaults should replace tuple param with config list value."""
    ctx = _make_click_context(
        params={"extra_window": (), "other_param": "value"},
    )

    # Create config with a list value for the tuple param
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"extra_window": ["cmd1", "cmd2"]})},
    )

    result = apply_config_defaults(ctx, config, "create")

    # List value should be used directly
    assert result["extra_window"] == ["cmd1", "cmd2"]


def test_apply_config_defaults_empty_string_does_not_affect_non_tuple_params(mngr_test_prefix: str) -> None:
    """apply_config_defaults should not convert empty string for non-tuple params."""
    ctx = _make_click_context(
        params={"name": "default_name", "other_param": "value"},
    )

    # Create config with empty string for the string param
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"name": ""})},
    )

    result = apply_config_defaults(ctx, config, "create")

    # Empty string should be kept as-is for non-tuple params
    assert result["name"] == ""


# Tests for apply_create_template


def test_apply_create_template_no_templates(mngr_test_prefix: str) -> None:
    """apply_create_template should return params unchanged when no templates specified."""
    ctx = _make_click_context(
        params={"template": (), "name": "default"},
    )
    params = ctx.params.copy()
    config = MngrConfig(prefix=mngr_test_prefix)

    result = apply_create_template(ctx, params, config)

    assert result == params


def test_apply_create_template_single_template(mngr_test_prefix: str) -> None:
    """apply_create_template should apply a single template's values."""
    ctx = _make_click_context(
        params={"template": ("mytemplate",), "type": None, "name": "default"},
    )

    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("mytemplate"): CreateTemplate(options={"type": "codex"}),
        },
    )

    result = apply_create_template(ctx, ctx.params.copy(), config)

    assert result["type"] == "codex"


def test_apply_create_template_multiple_templates_stack(mngr_test_prefix: str) -> None:
    """apply_create_template should stack multiple templates in order."""
    ctx = _make_click_context(
        params={
            "template": ("host-template", "agent-template"),
            "snapshot": None,
            "type": None,
            "name": "default",
        },
    )

    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("host-template"): CreateTemplate(options={"snapshot": "my-snapshot"}),
            CreateTemplateName("agent-template"): CreateTemplate(options={"type": "codex"}),
        },
    )

    result = apply_create_template(ctx, ctx.params.copy(), config)

    assert result["snapshot"] == "my-snapshot"
    assert result["type"] == "codex"


def test_apply_create_template_later_template_overrides_earlier(mngr_test_prefix: str) -> None:
    """apply_create_template should let later templates override earlier ones for the same key."""
    ctx = _make_click_context(
        params={
            "template": ("first", "second"),
            "type": None,
        },
    )

    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("first"): CreateTemplate(options={"type": "codex"}),
            CreateTemplateName("second"): CreateTemplate(options={"type": "claude"}),
        },
    )

    result = apply_create_template(ctx, ctx.params.copy(), config)

    assert result["type"] == "claude"


def test_apply_create_template_cli_args_override_all_templates(mngr_test_prefix: str) -> None:
    """apply_create_template should not override CLI-specified values even with multiple templates."""
    ctx = _make_click_context(
        params={
            "template": ("first", "second"),
            "type": "generic",
        },
        source_by_param_name={
            "type": ParameterSource.COMMANDLINE,
        },
    )

    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("first"): CreateTemplate(options={"type": "codex"}),
            CreateTemplateName("second"): CreateTemplate(options={"type": "claude"}),
        },
    )

    result = apply_create_template(ctx, ctx.params.copy(), config)

    assert result["type"] == "generic"


def test_apply_create_template_unknown_template_raises_error(mngr_test_prefix: str) -> None:
    """apply_create_template should raise UserInputError for unknown template."""
    ctx = _make_click_context(
        params={"template": ("nonexistent",)},
    )

    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("existing"): CreateTemplate(options={"type": "codex"}),
        },
    )

    with pytest.raises(UserInputError, match="Template 'nonexistent' not found"):
        apply_create_template(ctx, ctx.params.copy(), config)


def test_apply_create_template_second_template_unknown_raises_error(mngr_test_prefix: str) -> None:
    """apply_create_template should raise UserInputError if any template in the list is unknown."""
    ctx = _make_click_context(
        params={
            "template": ("existing", "nonexistent"),
            "type": None,
        },
    )

    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("existing"): CreateTemplate(options={"type": "codex"}),
        },
    )

    with pytest.raises(UserInputError, match="Template 'nonexistent' not found"):
        apply_create_template(ctx, ctx.params.copy(), config)


# =============================================================================
# Tests for _process_template_escapes
# =============================================================================


def test_process_template_escapes_tab() -> None:
    """_process_template_escapes should convert \\t to tab."""
    assert _process_template_escapes("{name}\\t{state}") == "{name}\t{state}"


def test_process_template_escapes_newline() -> None:
    """_process_template_escapes should convert \\n to newline."""
    assert _process_template_escapes("{name}\\n{state}") == "{name}\n{state}"


def test_process_template_escapes_carriage_return() -> None:
    """_process_template_escapes should convert \\r to carriage return."""
    assert _process_template_escapes("line\\r") == "line\r"


def test_process_template_escapes_literal_backslash() -> None:
    """_process_template_escapes should convert \\\\\\\\ to a single backslash."""
    assert _process_template_escapes("path\\\\file") == "path\\file"


def test_process_template_escapes_no_escapes() -> None:
    """_process_template_escapes should pass through strings without escapes."""
    assert _process_template_escapes("{name} {state}") == "{name} {state}"


def test_process_template_escapes_literal_backslash_before_t() -> None:
    """_process_template_escapes should treat \\\\t as literal backslash + t, not as tab."""
    assert _process_template_escapes("\\\\t") == "\\t"


# =============================================================================
# Tests for parse_output_options
# =============================================================================


def test_parse_output_options_quiet_sets_console_level_none(mngr_test_prefix: str) -> None:
    """parse_output_options should set console_level to NONE when quiet is True."""
    config = MngrConfig(prefix=mngr_test_prefix)
    output_opts, logging_config = parse_output_options(
        output_format="human",
        quiet=True,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        config=config,
    )
    assert logging_config.console_level == LogLevel.NONE
    assert output_opts.is_quiet is True


def test_parse_output_options_verbose_1_sets_debug(mngr_test_prefix: str) -> None:
    """parse_output_options should set console_level to DEBUG when verbose=1."""
    config = MngrConfig(prefix=mngr_test_prefix)
    output_opts, logging_config = parse_output_options(
        output_format="human",
        quiet=False,
        verbose=1,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        config=config,
    )
    assert logging_config.console_level == LogLevel.DEBUG


def test_parse_output_options_verbose_2_sets_trace(mngr_test_prefix: str) -> None:
    """parse_output_options should set console_level to TRACE when verbose>=2."""
    config = MngrConfig(prefix=mngr_test_prefix)
    output_opts, logging_config = parse_output_options(
        output_format="human",
        quiet=False,
        verbose=2,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        config=config,
    )
    assert logging_config.console_level == LogLevel.TRACE


def test_parse_output_options_format_template(mngr_test_prefix: str) -> None:
    """parse_output_options should recognize a non-builtin format as a template string."""
    config = MngrConfig(prefix=mngr_test_prefix)
    output_opts, logging_config = parse_output_options(
        output_format="{name}\\t{state}",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        config=config,
    )
    assert output_opts.output_format == OutputFormat.HUMAN
    assert output_opts.format_template == "{name}\t{state}"


def test_parse_output_options_invalid_template_raises(mngr_test_prefix: str) -> None:
    """parse_output_options should raise UsageError for invalid format templates."""
    config = MngrConfig(prefix=mngr_test_prefix)
    with pytest.raises(click.UsageError, match="Invalid format template"):
        parse_output_options(
            output_format="{unclosed",
            quiet=False,
            verbose=0,
            log_file=None,
            log_commands=None,
            log_command_output=None,
            log_env_vars=None,
            config=config,
        )


# =============================================================================
# Tests for apply_config_defaults edge cases
# =============================================================================


def test_apply_config_defaults_skips_unknown_param_names(mngr_test_prefix: str) -> None:
    """apply_config_defaults should skip config defaults for params not in context."""
    ctx = _make_click_context(
        params={"name": "default"},
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"nonexistent_param": "value", "name": "overridden"})},
    )
    result = apply_config_defaults(ctx, config, "create")
    assert result["name"] == "overridden"
    assert "nonexistent_param" not in result


# =============================================================================
# Tests for apply_create_template edge cases
# =============================================================================


def test_apply_create_template_unknown_template_no_templates_configured(mngr_test_prefix: str) -> None:
    """apply_create_template should raise UserInputError with helpful message when no templates exist."""
    ctx = _make_click_context(
        params={"template": ("nonexistent",)},
    )
    config = MngrConfig(prefix=mngr_test_prefix)

    with pytest.raises(UserInputError, match="No templates are configured"):
        apply_create_template(ctx, ctx.params.copy(), config)


def test_apply_create_template_skips_none_values(mngr_test_prefix: str) -> None:
    """apply_create_template should skip template values that are None."""
    ctx = _make_click_context(
        params={"template": ("mytemplate",), "new_host": None, "name": "default"},
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("mytemplate"): CreateTemplate(options={"new_host": None, "name": "from-template"}),
        },
    )
    result = apply_create_template(ctx, ctx.params.copy(), config)
    # new_host should remain None since the template value is None
    assert result["new_host"] is None
    # name should be overridden since its template value is not None
    assert result["name"] == "from-template"


def test_apply_create_template_skips_unknown_params(mngr_test_prefix: str) -> None:
    """apply_create_template should skip template params not in the original params dict."""
    ctx = _make_click_context(
        params={"template": ("mytemplate",), "name": "default"},
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("mytemplate"): CreateTemplate(options={"nonexistent_param": "value"}),
        },
    )
    result = apply_create_template(ctx, ctx.params.copy(), config)
    assert "nonexistent_param" not in result


# =============================================================================
# Tests for _split_known_and_plugin_params
# =============================================================================


def test_split_known_and_plugin_params_separates_known_from_extra() -> None:
    """_split_known_and_plugin_params should separate known fields from plugin params."""
    params = {
        "output_format": "human",
        "quiet": False,
        "verbose": 0,
        "log_file": None,
        "log_commands": None,
        "log_command_output": None,
        "log_env_vars": None,
        "project_context_path": None,
        "plugin": (),
        "disable_plugin": (),
        "test_plugin_option": "hello",
        "another_plugin_flag": True,
    }

    known, plugin = _split_known_and_plugin_params(params, CommonCliOptions)

    assert "output_format" in known
    assert "quiet" in known
    assert "test_plugin_option" not in known
    assert "another_plugin_flag" not in known

    assert "test_plugin_option" in plugin
    assert plugin["test_plugin_option"] == "hello"
    assert "another_plugin_flag" in plugin
    assert plugin["another_plugin_flag"] is True
    assert "output_format" not in plugin


def test_split_known_and_plugin_params_all_known() -> None:
    """_split_known_and_plugin_params should return empty plugin params when all are known."""
    params = {
        "output_format": "human",
        "quiet": False,
        "verbose": 0,
        "log_file": None,
        "log_commands": None,
        "log_command_output": None,
        "log_env_vars": None,
        "project_context_path": None,
        "plugin": (),
        "disable_plugin": (),
    }

    known, plugin = _split_known_and_plugin_params(params, CommonCliOptions)

    assert known == params
    assert plugin == {}


def test_split_known_and_plugin_params_empty_params() -> None:
    """_split_known_and_plugin_params should handle empty params dict."""
    known, plugin = _split_known_and_plugin_params({}, CommonCliOptions)

    assert known == {}
    assert plugin == {}


# =============================================================================
# Tests for --headless flag integration with setup_command_context
# =============================================================================


def test_headless_flag_sets_is_interactive_false_via_setup_command_context(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--headless CLI flag should result in is_interactive=False on MngrContext.

    This tests the full integration path: --headless flag -> CommonCliOptions.headless
    -> setup_command_context -> mngr_ctx.is_interactive=False.
    """
    captured_is_interactive: list[bool] = []

    @click.command()
    @add_common_options
    @click.pass_context
    def test_command(ctx: click.Context, **kwargs: Any) -> None:
        mngr_ctx, _output_opts, _opts = setup_command_context(
            ctx=ctx,
            command_name="test",
            command_class=CommonCliOptions,
        )
        captured_is_interactive.append(mngr_ctx.is_interactive)

    result = cli_runner.invoke(
        test_command,
        ["--headless"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert captured_is_interactive == [False]
