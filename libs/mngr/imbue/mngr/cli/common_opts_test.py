"""Tests for common_opts module."""

from pathlib import Path
from typing import Any

import click
import pluggy
import pytest
from click.core import ParameterSource
from click.testing import CliRunner

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.cli.common_opts import _apply_template_extend
from imbue.mngr.cli.common_opts import _process_template_escapes
from imbue.mngr.cli.common_opts import _run_pre_command_scripts
from imbue.mngr.cli.common_opts import _run_single_script
from imbue.mngr.cli.common_opts import _split_known_and_plugin_params
from imbue.mngr.cli.common_opts import add_common_options
from imbue.mngr.cli.common_opts import apply_config_defaults
from imbue.mngr.cli.common_opts import apply_create_template
from imbue.mngr.cli.common_opts import apply_settings_to_config
from imbue.mngr.cli.common_opts import parse_output_options
from imbue.mngr.cli.common_opts import restore_cli_list_values
from imbue.mngr.cli.common_opts import save_cli_list_values_for_restoration
from imbue.mngr.cli.common_opts import setup_command_context
from imbue.mngr.config.data_types import CommandDefaults
from imbue.mngr.config.data_types import CommonCliOptions
from imbue.mngr.config.data_types import CreateTemplate
from imbue.mngr.config.data_types import CreateTemplateName
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.key_resolver import set_at_path
from imbue.mngr.errors import ConfigParseError
from imbue.mngr.errors import UserInputError
from imbue.mngr.plugins import hookspecs
from imbue.mngr.primitives import LogLevel
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName

hookimpl = pluggy.HookimplMarker("mngr")


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
    script, exit_code, stdout, stderr = _run_single_script("echo hello", cg, cwd=None)
    assert script == "echo hello"
    assert exit_code == 0
    assert "hello" in stdout
    assert stderr == ""


def test_run_single_script_failure(cg: ConcurrencyGroup) -> None:
    """_run_single_script should return non-zero exit code for failed command."""
    script, exit_code, stdout, stderr = _run_single_script("exit 1", cg, cwd=None)
    assert script == "exit 1"
    assert exit_code == 1


def test_run_single_script_captures_stderr(cg: ConcurrencyGroup) -> None:
    """_run_single_script should capture stderr from failed command."""
    script, exit_code, stdout, stderr = _run_single_script("echo error >&2 && exit 1", cg, cwd=None)
    assert exit_code == 1
    assert "error" in stderr


def test_run_single_script_uses_cwd(tmp_path: Path, cg: ConcurrencyGroup) -> None:
    """_run_single_script should run the script in the specified cwd."""
    script, exit_code, stdout, stderr = _run_single_script("pwd", cg, cwd=tmp_path)
    assert exit_code == 0
    assert stdout.strip() == str(tmp_path)


def test_run_pre_command_scripts_uses_cwd(tmp_path: Path, mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should pass cwd to scripts."""
    marker = tmp_path / "marker.txt"
    config = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"create": ["touch marker.txt"]},
    )
    _run_pre_command_scripts(config, "create", cg, cwd=tmp_path)
    assert marker.exists()


def test_run_pre_command_scripts_no_scripts(mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should do nothing if no scripts configured."""
    config = MngrConfig(prefix=mngr_test_prefix, pre_command_scripts={})
    # Should not raise
    _run_pre_command_scripts(config, "create", cg, cwd=None)


def test_run_pre_command_scripts_no_scripts_for_command(mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should do nothing if no scripts for this command."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"other_command": ["echo hello"]},
    )
    # Should not raise
    _run_pre_command_scripts(config, "create", cg, cwd=None)


def test_run_pre_command_scripts_success(mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should succeed when all scripts pass."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"create": ["echo first", "echo second"]},
    )
    # Should not raise
    _run_pre_command_scripts(config, "create", cg, cwd=None)


def test_run_pre_command_scripts_single_failure(mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should raise ClickException when a script fails."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"create": ["exit 1"]},
    )
    with pytest.raises(click.ClickException) as exc_info:
        _run_pre_command_scripts(config, "create", cg, cwd=None)
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
        _run_pre_command_scripts(config, "create", cg, cwd=None)
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
        _run_pre_command_scripts(config, "create", cg, cwd=None)
    assert "Exit code: 42" in str(exc_info.value)


def test_run_pre_command_scripts_includes_stderr_in_error(mngr_test_prefix: str, cg: ConcurrencyGroup) -> None:
    """_run_pre_command_scripts should include stderr in error message."""
    config = MngrConfig(
        prefix=mngr_test_prefix,
        pre_command_scripts={"create": ["echo 'my error message' >&2 && exit 1"]},
    )
    with pytest.raises(click.ClickException) as exc_info:
        _run_pre_command_scripts(config, "create", cg, cwd=None)
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
    """apply_config_defaults substitutes the config list as the tuple param's base.

    Click's multi-option params are tuples on the receiving side, so the config
    value is coerced to a tuple here for shape consistency with the rest of the
    pipeline (templates, restore_cli_list_values both produce tuples).
    """
    ctx = _make_click_context(
        params={"extra_window": (), "other_param": "value"},
    )

    # Create config with a list value for the tuple param
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"extra_window": ["cmd1", "cmd2"]})},
    )

    result = apply_config_defaults(ctx, config, "create")

    assert result["extra_window"] == ("cmd1", "cmd2")


def test_apply_config_defaults_substitutes_config_base_for_cli_tuple_params(mngr_test_prefix: str) -> None:
    """apply_config_defaults now substitutes the config value as the BASE for every
    tuple/list param, regardless of CLI source.

    Under the new pipeline order (config_defaults -> templates -> CLI extension),
    templates need to see a clean config base, not config+CLI mixed together.
    The CLI tuple value is saved separately by
    ``save_cli_list_values_for_restoration`` and appended at the end of the
    pipeline by ``restore_cli_list_values``.
    """
    ctx = _make_click_context(
        params={"env": ("X=6",), "other_param": "value"},
        source_by_param_name={"env": ParameterSource.COMMANDLINE},
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=5"]})},
    )
    result = apply_config_defaults(ctx, config, "create")
    # Config value is the base; the CLI value will be re-added at the end of the pipeline.
    assert result["env"] == ("X=5",)


def test_apply_config_defaults_leaves_cli_source_scalars_untouched(mngr_test_prefix: str) -> None:
    """CLI-source scalar params keep their CLI value through apply_config_defaults
    (no config-base substitution for scalars; templates also skip them)."""
    ctx = _make_click_context(
        params={"new_host": "cli-host"},
        source_by_param_name={"new_host": ParameterSource.COMMANDLINE},
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"new_host": "config-host"})},
    )
    result = apply_config_defaults(ctx, config, "create")
    assert result["new_host"] == "cli-host"


def test_pipeline_cli_flag_extends_non_empty_config(mngr_test_prefix: str) -> None:
    """End-to-end pipeline test: a CLI tuple flag extends the merged settings value.

    Order is ``config + CLI`` so the CLI value reads as the user's final word
    at the tail of the list. With the new assign-by-default merge for settings
    files, a single non-empty ``commands.create.env`` ends up in the merged
    config (e.g. ``["X=5"]`` from the local layer wiping the project layer's
    ``["X=4"]``). The CLI flag still extends that result.
    """
    ctx = _make_click_context(
        params={"env": ("X=6",), "other_param": "value"},
        source_by_param_name={"env": ParameterSource.COMMANDLINE},
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=5"]})},
    )
    after_defaults = apply_config_defaults(ctx, config, "create")
    cli_values = save_cli_list_values_for_restoration(ctx)
    # No templates here, so apply_create_template would be a no-op.
    result = restore_cli_list_values(after_defaults, cli_values)
    assert result["env"] == ("X=5", "X=6")


def test_pipeline_cli_flag_extends_multiple_values(mngr_test_prefix: str) -> None:
    """Multiple CLI flag invocations all append after the config-supplied entries."""
    ctx = _make_click_context(
        params={"env": ("X=6", "X=7"), "other_param": "value"},
        source_by_param_name={"env": ParameterSource.COMMANDLINE},
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=5"]})},
    )
    after_defaults = apply_config_defaults(ctx, config, "create")
    cli_values = save_cli_list_values_for_restoration(ctx)
    result = restore_cli_list_values(after_defaults, cli_values)
    assert result["env"] == ("X=5", "X=6", "X=7")


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
            config=config,
        )


# =============================================================================
# Tests for apply_config_defaults edge cases
# =============================================================================


def test_apply_config_defaults_raises_on_unknown_param_names(mngr_test_prefix: str) -> None:
    """apply_config_defaults should raise for params not in context when strict=True."""
    ctx = _make_click_context(
        params={"name": "default"},
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"nonexistent_param": "value", "name": "overridden"})},
    )
    with pytest.raises(ConfigParseError, match="nonexistent_param"):
        apply_config_defaults(ctx, config, "create", strict=True)


@pytest.mark.allow_warnings(match=r"Unknown parameter 'definitely_not_a_real_param'")
def test_apply_config_defaults_warns_on_unknown_param_when_lax(
    mngr_test_prefix: str,
    log_warnings: list[str],
) -> None:
    """apply_config_defaults should warn (not raise) when strict=False."""
    ctx = _make_click_context(params={"name": "default"})

    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"definitely_not_a_real_param": "x"})},
    )

    # Should not raise; the unknown param should produce a warning instead.
    result = apply_config_defaults(ctx, config, "create", strict=False)
    assert "definitely_not_a_real_param" not in result
    assert any("definitely_not_a_real_param" in msg for msg in log_warnings), (
        f"Expected a warning mentioning the unknown param, got: {log_warnings}"
    )


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
# Tests for apply_create_template list/tuple merging
# =============================================================================


def test_apply_create_template_narrowing_raises_against_config_defaults(mngr_test_prefix: str) -> None:
    """A template that bare-assigns a list option over a non-empty config-default
    value drops the prior entries and trips the narrowing guard."""
    ctx = _make_click_context(
        params={
            "template": ("main",),
            "env": ("IS_SANDBOX=1", "IS_AUTONOMOUS=1"),
        },
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("main"): CreateTemplate(options={"env": ["REVIEWER_AUTOFIX_ENABLE=0"]}),
        },
    )
    with pytest.raises(ConfigParseError, match="narrowing"):
        apply_create_template(ctx, ctx.params.copy(), config)


def test_apply_create_template_extend_appends_to_config_defaults(mngr_test_prefix: str) -> None:
    """``env__extend = [...]`` in a template is the documented opt-in for additive
    behavior; it appends to the existing value without tripping the narrowing guard.
    """
    ctx = _make_click_context(
        params={
            "template": ("main",),
            "env": ("IS_SANDBOX=1", "IS_AUTONOMOUS=1"),
        },
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("main"): CreateTemplate(options={"env__extend": ["REVIEWER_AUTOFIX_ENABLE=0"]}),
        },
    )
    result = apply_create_template(ctx, ctx.params.copy(), config)
    assert result["env"] == ("IS_SANDBOX=1", "IS_AUTONOMOUS=1", "REVIEWER_AUTOFIX_ENABLE=0")


def test_apply_create_template_assign_with_opt_in_replaces(mngr_test_prefix: str) -> None:
    """With ``allow_settings_key_assignment_narrowing = true``, a template's bare
    list assign is allowed and replaces the existing value (assign-by-default)."""
    ctx = _make_click_context(
        params={
            "template": ("main",),
            "env": ("FROM_CONFIG=1",),
        },
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        allow_settings_key_assignment_narrowing=True,
        create_templates={
            CreateTemplateName("main"): CreateTemplate(options={"env": ["TEMPLATE=1"]}),
        },
    )
    result = apply_create_template(ctx, ctx.params.copy(), config)
    assert result["env"] == ("TEMPLATE=1",)


def test_apply_create_template_multiple_templates_extend_stack(mngr_test_prefix: str) -> None:
    """Multiple templates each using ``env__extend`` stack additively, in template-order.

    Combined with the pipeline ordering (config defaults -> templates -> CLI extend),
    the final list reads chronologically: earlier layers first, later layers last.
    """
    ctx = _make_click_context(
        params={
            "template": ("first", "second"),
            "env": (),
        },
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("first"): CreateTemplate(options={"env__extend": ["FOO=1"]}),
            CreateTemplateName("second"): CreateTemplate(options={"env__extend": ["BAR=2"]}),
        },
    )
    result = apply_create_template(ctx, ctx.params.copy(), config)
    assert result["env"] == ("FOO=1", "BAR=2")


def test_apply_create_template_post_host_create_command_extend_stacks(mngr_test_prefix: str) -> None:
    """`post_host_create_command__extend` from a template merges into the CLI tuple param
    so users can opt into image-specific first-boot setup (e.g. FCT's /usr/local/bin/fct-seed)
    without inlining shell into mngr."""
    ctx = _make_click_context(
        params={
            "template": ("fct-docker",),
            "post_host_create_command": (),
        },
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("fct-docker"): CreateTemplate(
                options={"post_host_create_command__extend": ["/usr/local/bin/fct-seed"]}
            ),
        },
    )
    result = apply_create_template(ctx, ctx.params.copy(), config)
    assert result["post_host_create_command"] == ("/usr/local/bin/fct-seed",)


def test_apply_create_template_second_template_narrowing_raises(mngr_test_prefix: str) -> None:
    """When the first template extends and the second bare-assigns over the result,
    the second template trips the narrowing guard against the in-flight value."""
    ctx = _make_click_context(
        params={
            "template": ("first", "second"),
            "env": (),
        },
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("first"): CreateTemplate(options={"env__extend": ["FOO=1"]}),
            CreateTemplateName("second"): CreateTemplate(options={"env": ["BAR=2"]}),
        },
    )
    with pytest.raises(ConfigParseError, match="narrowing"):
        apply_create_template(ctx, ctx.params.copy(), config)


def test_apply_create_template_empty_list_assign_raises_without_opt_in(mngr_test_prefix: str) -> None:
    """An explicit empty-list assign in a template (over a non-empty existing value)
    is the most extreme narrowing case and raises by default. Symmetrical with
    the settings-layer guard, which also flags ``env = []`` over a non-empty base.
    """
    ctx = _make_click_context(
        params={
            "template": ("reset-template",),
            "env": ("FROM_CONFIG=1",),
        },
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        create_templates={
            CreateTemplateName("reset-template"): CreateTemplate(options={"env": []}),
        },
    )
    with pytest.raises(ConfigParseError, match="narrowing"):
        apply_create_template(ctx, ctx.params.copy(), config)


def test_apply_create_template_empty_list_assign_with_opt_in_clears(mngr_test_prefix: str) -> None:
    """With the opt-in flag, an empty-list template assign clears the existing value."""
    ctx = _make_click_context(
        params={
            "template": ("reset-template",),
            "env": ("FROM_CONFIG=1",),
        },
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        allow_settings_key_assignment_narrowing=True,
        create_templates={
            CreateTemplateName("reset-template"): CreateTemplate(options={"env": []}),
        },
    )
    result = apply_create_template(ctx, ctx.params.copy(), config)
    assert result["env"] == ()


def test_apply_create_template_template_then_cli_via_pipeline(mngr_test_prefix: str) -> None:
    """End-to-end pipeline: templates run first (with CLI list values temporarily
    set aside), then the CLI value extends the template result at the end.

    So if a template extends config defaults to ``("X=1", "X=3")`` and the CLI
    supplies ``--env X=6``, the final value is ``("X=1", "X=3", "X=6")``.
    """
    ctx = _make_click_context(
        params={
            "template": ("dev",),
            "env": ("X=6",),
        },
        source_by_param_name={"env": ParameterSource.COMMANDLINE},
    )
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=1"]})},
        create_templates={
            CreateTemplateName("dev"): CreateTemplate(options={"env__extend": ["X=3"]}),
        },
    )
    after_defaults = apply_config_defaults(ctx, config, "create")
    cli_values = save_cli_list_values_for_restoration(ctx)
    after_templates = apply_create_template(ctx, after_defaults, config)
    result = restore_cli_list_values(after_templates, cli_values)
    assert result["env"] == ("X=1", "X=3", "X=6")


def test_apply_create_template_scalar_overrides_default_param(mngr_test_prefix: str) -> None:
    """Scalar template options assign-by-default for DEFAULT-source params (existing
    behaviour, regression-test pinned: scalars don't go through the narrowing guard)."""
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


def test_apply_template_extend_dict_merges_keys() -> None:
    """``key__extend = {...}`` on a dict-typed value merges keys, preserving siblings
    not mentioned in the extend value (matches the shared ``apply_extend`` semantics).

    No CreateCliOptions field is dict-typed today, so this exercises
    ``_apply_template_extend`` directly: the helper delegates to ``apply_extend``
    and must stay shape-correct for the dict branch in case a future option uses one.
    """
    result = _apply_template_extend(
        {"a": "1"},
        {"b": "2"},
        template_name="dev",
        param_name="example_dict_field",
    )
    assert result == {"a": "1", "b": "2"}


def test_apply_template_extend_dict_recurses_into_nested_extend() -> None:
    """A nested ``key__extend`` inside the dict extend value recurses rather than
    shallow-replacing the nested value (the recursive fix over the prior shallow merge)."""
    result = _apply_template_extend(
        {"allow": ["old"], "defaultMode": "acceptEdits"},
        {"allow__extend": ["new"]},
        template_name="dev",
        param_name="permissions",
    )
    assert result == {"allow": ["old", "new"], "defaultMode": "acceptEdits"}


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


# =============================================================================
# Tests for the shared set_at_path helper used by --setting parsing
#
# Value-parsing semantics (boolean, integer, JSON array, etc.) are exercised in
# the dedicated key_resolver_test suite that owns ``parse_scalar_value``; we
# don't re-test them here since ``apply_settings_to_config`` now calls that
# shared helper directly rather than through a per-callsite wrapper.
# =============================================================================


def test_set_at_path_single_segment() -> None:
    """set_at_path should set a top-level key."""
    data: dict[str, Any] = {}
    set_at_path(data, ["prefix"], "my-")
    assert data == {"prefix": "my-"}


def test_set_at_path_nested_segments() -> None:
    """set_at_path should create intermediate dicts for nested segment paths."""
    data: dict[str, Any] = {}
    set_at_path(data, ["commands", "create", "connect"], False)
    assert data == {"commands": {"create": {"connect": False}}}


def test_set_at_path_preserves_existing_siblings() -> None:
    """set_at_path should preserve existing sibling keys at each level."""
    data: dict[str, Any] = {"commands": {"create": {"branch": "main"}}}
    set_at_path(data, ["commands", "create", "connect"], False)
    assert data == {"commands": {"create": {"branch": "main", "connect": False}}}


def test_set_at_path_overwrites_non_dict_intermediate() -> None:
    """set_at_path should replace a non-dict intermediate value with a fresh dict."""
    data: dict[str, Any] = {"commands": "not-a-dict"}
    set_at_path(data, ["commands", "create", "connect"], False)
    assert data == {"commands": {"create": {"connect": False}}}


# =============================================================================
# Tests for apply_settings_to_config
# =============================================================================


def test_apply_settings_to_config_empty_settings(mngr_test_prefix: str) -> None:
    """apply_settings_to_config should return config unchanged when settings is empty."""
    config = MngrConfig(prefix=mngr_test_prefix)
    result = apply_settings_to_config(config, (), frozenset())
    assert result.prefix == mngr_test_prefix


def test_apply_settings_to_config_sets_scalar(mngr_test_prefix: str) -> None:
    """apply_settings_to_config should override a scalar config field."""
    config = MngrConfig(prefix=mngr_test_prefix)
    result = apply_settings_to_config(config, ("prefix=custom-",), frozenset())
    assert result.prefix == "custom-"


def test_apply_settings_to_config_sets_command_defaults(mngr_test_prefix: str) -> None:
    """apply_settings_to_config should set command defaults via dotted paths."""
    config = MngrConfig(prefix=mngr_test_prefix)
    result = apply_settings_to_config(
        config,
        ("commands.create.connect=false",),
        frozenset(),
    )
    assert result.commands["create"].defaults["connect"] is False


def test_apply_settings_to_config_replaces_existing_command_defaults(mngr_test_prefix: str) -> None:
    """Assign-by-default: --setting on a command param replaces the whole defaults map.

    To preserve other keys, the user would explicitly write ``defaults__extend``
    or repeat each key in the --setting list. The narrowing guard is opted out
    of via ``allow_settings_key_assignment_narrowing=True`` so the test exercises
    the assign-by-default behavior directly; without the opt-in this would raise
    a ConfigParseError (see ``test_apply_settings_to_config_narrowing_raises``).
    """
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"branch": "main:agent/*"})},
        allow_settings_key_assignment_narrowing=True,
    )
    result = apply_settings_to_config(
        config,
        ("commands.create.connect=false",),
        frozenset(),
    )
    # Only the new setting's key is present; the prior "branch" entry was wiped.
    assert result.commands["create"].defaults == {"connect": False}


def test_apply_settings_to_config_narrowing_raises_by_default(mngr_test_prefix: str) -> None:
    """Without the opt-in, a --setting that would drop earlier entries raises ConfigParseError.

    Mirrors the test above but uses the default ``allow_settings_key_assignment_narrowing=False``,
    which is the safety net for users who haven't migrated to the new assign-by-default behavior.
    """
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"branch": "main:agent/*"})},
    )
    with pytest.raises(ConfigParseError, match="narrowing"):
        apply_settings_to_config(
            config,
            ("commands.create.connect=false",),
            frozenset(),
        )


def test_apply_settings_to_config_clear_raises_without_opt_in(mngr_test_prefix: str) -> None:
    """``--setting commands.create.env=[]`` over a non-empty base raises by default.

    Clearing is the most extreme form of data loss (every prior entry is dropped),
    so the narrowing guard treats it the same as any other assign that loses
    entries. The user must set ``allow_settings_key_assignment_narrowing=True``
    (or switch to ``__extend``) to make the loss explicit.
    """
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=5"]})},
    )
    with pytest.raises(ConfigParseError, match="narrowing"):
        apply_settings_to_config(
            config,
            ("commands.create.env=[]",),
            frozenset(),
        )


def test_apply_settings_to_config_clear_then_cli_flag_replaces_with_opt_in(mngr_test_prefix: str) -> None:
    """With the opt-in, ``--setting commands.create.env=[]`` clears the merged
    value; a later CLI ``--env X=6`` then has nothing to extend and becomes the
    only entry.

    Order in setup_command_context: ``apply_settings_to_config`` runs first
    (turning ``commands.create.env`` into ``[]`` via assign), then
    ``apply_config_defaults`` sees an empty config value and falls through to
    the CLI value. Empty config value never extends, so ``["X=6"]`` is the result.
    """
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=5"]})},
        allow_settings_key_assignment_narrowing=True,
    )
    cleared = apply_settings_to_config(
        config,
        ("commands.create.env=[]",),
        frozenset(),
    )
    assert cleared.commands["create"].defaults["env"] == []
    ctx = _make_click_context(
        params={"env": ("X=6",)},
        source_by_param_name={"env": ParameterSource.COMMANDLINE},
    )
    # Full pipeline: apply_config_defaults sets the config base (empty here);
    # save_cli_list_values_for_restoration stashes the CLI value;
    # restore_cli_list_values appends the CLI value at the end.
    after_defaults = apply_config_defaults(ctx, cleared, "create")
    cli_values = save_cli_list_values_for_restoration(ctx)
    result = restore_cli_list_values(after_defaults, cli_values)
    assert result["env"] == ("X=6",)


def test_apply_settings_to_config_extend_then_cli_flag_appends(mngr_test_prefix: str) -> None:
    """``--setting commands.create.env__extend=["X=7"] --env X=6`` produces
    ``["X=5", "X=7", "X=6"]``: setting extends first, then CLI appends at the
    end of the pipeline via ``restore_cli_list_values``.
    """
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=5"]})},
    )
    extended = apply_settings_to_config(
        config,
        ('commands.create.env__extend=["X=7"]',),
        frozenset(),
    )
    assert extended.commands["create"].defaults["env"] == ["X=5", "X=7"]
    ctx = _make_click_context(
        params={"env": ("X=6",)},
        source_by_param_name={"env": ParameterSource.COMMANDLINE},
    )
    after_defaults = apply_config_defaults(ctx, extended, "create")
    cli_values = save_cli_list_values_for_restoration(ctx)
    # No templates here, so apply_create_template would be a no-op anyway.
    result = restore_cli_list_values(after_defaults, cli_values)
    assert result["env"] == ("X=5", "X=7", "X=6")


def test_apply_settings_to_config_extend_through_command_defaults(mngr_test_prefix: str) -> None:
    """``--setting commands.<name>.<param>__extend=...`` extends through the CommandDefaults wrapper.

    Without the resolver's CommandDefaults transparency, ``__extend`` would silently
    fall through to ``None`` for ``commands.<name>.<param>`` paths and become a
    plain assign, defeating the user's intent to extend the merged value.
    """
    config = MngrConfig(
        prefix=mngr_test_prefix,
        commands={"create": CommandDefaults(defaults={"env": ["X=5"]})},
    )
    result = apply_settings_to_config(
        config,
        ('commands.create.env__extend=["X=7"]',),
        frozenset(),
    )
    assert result.commands["create"].defaults["env"] == ["X=5", "X=7"]


def test_apply_settings_to_config_extends_list_field(mngr_test_prefix: str) -> None:
    """``--setting unset_vars__extend=...`` appends to the base list without wiping it."""
    config = MngrConfig(prefix=mngr_test_prefix, unset_vars=["BASE_VAR"])
    result = apply_settings_to_config(
        config,
        ('unset_vars__extend=["FROM_SETTING"]',),
        frozenset(),
    )
    assert result.unset_vars == ["BASE_VAR", "FROM_SETTING"]


def test_apply_settings_to_config_extend_on_scalar_raises(mngr_test_prefix: str) -> None:
    """``__extend`` is not valid on a scalar field; the overlay resolver's ``OverlayError``
    is translated to a ``ConfigParseError`` at the config boundary."""
    config = MngrConfig(prefix=mngr_test_prefix)
    with pytest.raises(ConfigParseError, match="__extend on field 'prefix'"):
        apply_settings_to_config(
            config,
            ("prefix__extend=oops",),
            frozenset(),
        )


def test_apply_settings_to_config_multiple_settings(mngr_test_prefix: str) -> None:
    """apply_settings_to_config should handle multiple setting strings."""
    config = MngrConfig(prefix=mngr_test_prefix)
    result = apply_settings_to_config(
        config,
        ("prefix=new-", "headless=true"),
        frozenset(),
    )
    assert result.prefix == "new-"
    assert result.headless is True


def test_apply_settings_to_config_sets_logging(mngr_test_prefix: str) -> None:
    """apply_settings_to_config should set nested logging config."""
    config = MngrConfig(prefix=mngr_test_prefix)
    result = apply_settings_to_config(
        config,
        ("logging.console_level=TRACE",),
        frozenset(),
    )
    assert result.logging.console_level == LogLevel.TRACE


def test_apply_settings_to_config_invalid_format_raises() -> None:
    """apply_settings_to_config should raise UserInputError for missing '=' sign."""
    config = MngrConfig(prefix="test-")
    with pytest.raises(UserInputError, match="Invalid --setting format"):
        apply_settings_to_config(config, ("bad-setting",), frozenset())


def test_apply_settings_to_config_empty_key_raises() -> None:
    """apply_settings_to_config should raise UserInputError for empty key."""
    config = MngrConfig(prefix="test-")
    with pytest.raises(UserInputError, match="key cannot be empty"):
        apply_settings_to_config(config, ("=value",), frozenset())


def test_apply_settings_to_config_unknown_field_raises() -> None:
    """apply_settings_to_config should raise ConfigParseError for unknown top-level keys."""
    config = MngrConfig(prefix="test-")
    with pytest.raises(ConfigParseError, match="Unknown configuration fields"):
        apply_settings_to_config(config, ("totally_bogus_key=value",), frozenset())


# =============================================================================
# Tests for --setting flag integration with setup_command_context
# =============================================================================


def test_setting_flag_overrides_config_via_setup_command_context(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--setting should override config values in the loaded MngrContext."""
    captured_prefix: list[str] = []

    @click.command()
    @add_common_options
    @click.pass_context
    def test_command(ctx: click.Context, **kwargs: Any) -> None:
        mngr_ctx, _output_opts, _opts = setup_command_context(
            ctx=ctx,
            command_name="test",
            command_class=CommonCliOptions,
        )
        captured_prefix.append(mngr_ctx.config.prefix)

    result = cli_runner.invoke(
        test_command,
        ["--setting", "prefix=my-custom-prefix-"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert captured_prefix == ["my-custom-prefix-"]


def test_setting_flag_repeatable_via_setup_command_context(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Multiple --setting flags should all be applied."""
    captured: list[tuple[str, bool]] = []

    @click.command()
    @add_common_options
    @click.pass_context
    def test_command(ctx: click.Context, **kwargs: Any) -> None:
        mngr_ctx, _output_opts, _opts = setup_command_context(
            ctx=ctx,
            command_name="test",
            command_class=CommonCliOptions,
        )
        captured.append((mngr_ctx.config.prefix, mngr_ctx.config.headless))

    result = cli_runner.invoke(
        test_command,
        ["-S", "prefix=x-", "-S", "headless=true"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert captured == [("x-", True)]


def test_setting_flag_sets_command_defaults_in_config(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--setting for command defaults should appear in the config's commands dict."""
    captured_config: list[MngrConfig] = []

    @click.command()
    @add_common_options
    @click.pass_context
    def test_command(ctx: click.Context, **kwargs: Any) -> None:
        mngr_ctx, _output_opts, _opts = setup_command_context(
            ctx=ctx,
            command_name="test",
            command_class=CommonCliOptions,
        )
        captured_config.append(mngr_ctx.config)

    result = cli_runner.invoke(
        test_command,
        ["--setting", "commands.create.connect=false"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert captured_config[0].commands["create"].defaults["connect"] is False


# =============================================================================
# Tests for create-template `setting`/`setting__extend` reaching the config
# (regression for: template-provided settings were silently dropped).
# =============================================================================


def _make_create_command_capturing_config() -> tuple[click.Command, list[MngrConfig]]:
    """Build a create-like click command and a sink that captures the resolved config.

    The command carries a ``--template`` option (so create templates resolve) plus
    the common options (so ``-S`` is parsed), and runs ``setup_command_context`` for
    the ``create`` command.
    """
    captured_config: list[MngrConfig] = []

    @click.command()
    @click.option("--template", multiple=True, default=())
    @add_common_options
    @click.pass_context
    def test_create(ctx: click.Context, **kwargs: Any) -> None:
        mngr_ctx, _output_opts, _opts = setup_command_context(
            ctx=ctx,
            command_name="create",
            command_class=CommonCliOptions,
        )
        captured_config.append(mngr_ctx.config)

    return test_create, captured_config


def test_create_template_setting_extend_lands_in_config(
    cli_runner: CliRunner,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create template's ``setting__extend`` should reach the resolved config."""
    (tmp_path / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[create_templates.tmpl]\nsetting__extend = ["prefix=tmpl-custom-"]\n'
    )
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    cmd, captured_config = _make_create_command_capturing_config()
    result = cli_runner.invoke(cmd, ["--template", "tmpl"], obj=plugin_manager, catch_exceptions=False)

    assert result.exit_code == 0, f"output={result.output!r} exception={result.exception!r}"
    assert captured_config[0].prefix == "tmpl-custom-"


def test_create_template_setting_bare_assign_lands_in_config(
    cli_runner: CliRunner,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create template's bare ``setting`` (assign) should reach the resolved config."""
    (tmp_path / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[create_templates.tmpl]\nsetting = ["prefix=tmpl-bare-"]\n'
    )
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    cmd, captured_config = _make_create_command_capturing_config()
    result = cli_runner.invoke(cmd, ["--template", "tmpl"], obj=plugin_manager, catch_exceptions=False)

    assert result.exit_code == 0, f"output={result.output!r} exception={result.exception!r}"
    assert captured_config[0].prefix == "tmpl-bare-"


def test_create_template_provider_subclass_setting_lands_in_config(
    cli_runner: CliRunner,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create template setting targeting a provider-subclass field reaches the config.

    This mirrors the original bug report (``providers.docker.docker_runtime`` set
    via a template ``setting__extend`` was silently ignored), using the local
    provider's subclass-only ``host_dir`` field so the test does not depend on the
    docker backend being registered in the unit-test plugin manager. The provider
    instance is named after its backend (``local``) because a ``setting`` delta
    that omits ``backend`` resolves the backend from the instance name -- the same
    convention the original docker repro relies on.
    """
    (tmp_path / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        '[providers.local]\nbackend = "local"\n\n'
        '[create_templates.tmpl]\nsetting__extend = ["providers.local.host_dir=/tmp/mngr-template-probe"]\n'
    )
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    cmd, captured_config = _make_create_command_capturing_config()
    result = cli_runner.invoke(cmd, ["--template", "tmpl"], obj=plugin_manager, catch_exceptions=False)

    assert result.exit_code == 0, f"output={result.output!r} exception={result.exception!r}"
    # The stored instance is a LocalProviderConfig; dump it (its concrete type
    # carries host_dir) to read the subclass-only field without a type error.
    local_config = captured_config[0].providers[ProviderInstanceName("local")]
    assert local_config.model_dump(mode="json")["host_dir"] == "/tmp/mngr-template-probe"


def test_cli_setting_wins_over_create_template_setting(
    cli_runner: CliRunner,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct CLI ``-S`` for the same key beats the create template's setting."""
    (tmp_path / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[create_templates.tmpl]\nsetting__extend = ["prefix=from-template-"]\n'
    )
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    cmd, captured_config = _make_create_command_capturing_config()
    result = cli_runner.invoke(
        cmd,
        ["--template", "tmpl", "-S", "prefix=from-cli-"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"output={result.output!r} exception={result.exception!r}"
    assert captured_config[0].prefix == "from-cli-"


def test_create_template_setting_targeting_command_defaults_raises(
    cli_runner: CliRunner,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template ``setting`` for ``commands.*`` cannot take effect, so it must raise."""
    (tmp_path / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[create_templates.tmpl]\nsetting__extend = ["commands.create.connect=false"]\n'
    )
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    cmd, _captured_config = _make_create_command_capturing_config()
    result = cli_runner.invoke(cmd, ["--template", "tmpl"], obj=plugin_manager, catch_exceptions=True)

    assert result.exit_code != 0
    assert "commands.create.connect" in result.output
    assert "cannot take effect" in result.output


def test_create_template_setting_targeting_create_templates_raises(
    cli_runner: CliRunner,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template ``setting`` for ``create_templates.*`` cannot take effect, so it must raise."""
    (tmp_path / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n\n"
        '[create_templates.tmpl]\nsetting__extend = ["create_templates.other.provider=docker"]\n'
    )
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    cmd, _captured_config = _make_create_command_capturing_config()
    result = cli_runner.invoke(cmd, ["--template", "tmpl"], obj=plugin_manager, catch_exceptions=True)

    assert result.exit_code != 0
    assert "create_templates.other.provider" in result.output


def test_cli_setting_for_command_defaults_still_works_on_create(
    cli_runner: CliRunner,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct CLI ``-S commands.*`` keeps working on create (the rejection is template-only)."""
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    cmd, captured_config = _make_create_command_capturing_config()
    result = cli_runner.invoke(
        cmd,
        ["-S", "commands.create.headless=true"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"output={result.output!r} exception={result.exception!r}"
    assert captured_config[0].commands["create"].defaults["headless"] is True


def test_create_template_setting_narrowing_raises(
    cli_runner: CliRunner,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A template ``setting`` that narrows a non-empty list raises, like ``--setting``."""
    (tmp_path / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n"
        'enabled_backends = ["docker"]\n\n'
        "[create_templates.tmpl]\nsetting__extend = ['enabled_backends=[]']\n"
    )
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    cmd, _captured_config = _make_create_command_capturing_config()
    result = cli_runner.invoke(cmd, ["--template", "tmpl"], obj=plugin_manager, catch_exceptions=True)

    assert result.exit_code != 0
    assert "narrowing" in result.output.lower()


def test_disable_plugin_in_command_defaults_blocks_override_hook(
    cli_runner: CliRunner,
    tmp_path: Path,
) -> None:
    """A plugin disabled via [commands.create] disable_plugin should not have
    its override_command_options hook fire.

    Regression test: the ttyd plugin was injecting a duplicate 'terminal'
    extra_window even though disable_plugin=["ttyd"] was set in the project
    settings under [commands.create]. The root cause was that disable_plugin
    from command defaults only updated CLI params but never called
    block_disabled_plugins on the plugin manager, so the plugin's hooks
    still fired.
    """

    class FakeTerminalPlugin:
        @hookimpl
        def override_command_options(
            self,
            command_name: str,
            command_class: type,
            params: dict[str, Any],
        ) -> None:
            if command_name == "create":
                existing = params.get("extra_window", ())
                params["extra_window"] = (*existing, 'terminal="fake-ttyd-command"')

    # Set up a project settings file that disables our plugin via command defaults
    project_dir = tmp_path / ".mngr"
    project_dir.mkdir(exist_ok=True)
    settings_path = project_dir / "settings.toml"
    settings_path.write_text('[commands.create]\ndisable_plugin = ["fake_terminal"]\n')

    pm = pluggy.PluginManager("mngr")
    pm.add_hookspecs(hookspecs)
    pm.register(FakeTerminalPlugin(), name="fake_terminal")

    captured_params: list[dict[str, Any]] = []

    @click.command()
    @click.option("--extra-window", multiple=True, default=())
    @add_common_options
    @click.pass_context
    def test_create(ctx: click.Context, **kwargs: Any) -> None:
        _mngr_ctx, _output_opts, _opts = setup_command_context(
            ctx=ctx,
            command_name="create",
            command_class=CommonCliOptions,
        )
        captured_params.append(ctx.params.copy())

    result = cli_runner.invoke(
        test_create,
        [],
        obj=pm,
        catch_exceptions=False,
        env={"MNGR_PROJECT_CONFIG_DIR": str(tmp_path)},
    )
    assert result.exit_code == 0, f"output={result.output!r} exception={result.exception!r}"
    assert len(captured_params) == 1

    extra_window = captured_params[0].get("extra_window", ())
    terminal_entries = [e for e in extra_window if "fake-ttyd" in str(e)]
    assert terminal_entries == [], (
        f"Disabled plugin's override_command_options hook still fired, "
        f"injecting: {terminal_entries}. extra_window={extra_window}"
    )


# =============================================================================
# Integration tests: MNGR_ALLOW_UNKNOWN_CONFIG threaded through setup_command_context
# =============================================================================


def _make_strict_test_command() -> click.Command:
    """Build a click command that runs setup_command_context for the 'create' command."""

    @click.command()
    @add_common_options
    @click.pass_context
    def cmd(ctx: click.Context, **kwargs: Any) -> None:
        setup_command_context(
            ctx=ctx,
            command_name="create",
            command_class=CommonCliOptions,
        )

    return cmd


def test_setup_command_context_raises_on_unknown_command_param_by_default(
    cli_runner: CliRunner,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without MNGR_ALLOW_UNKNOWN_CONFIG, a typo in [commands.create] must raise."""
    # MNGR_PROJECT_CONFIG_DIR points directly at the directory containing settings.toml
    # (see resolve_project_config_dir in config/pre_readers.py).
    (tmp_path / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[commands.create]\nbogus_typo_param = "x"\n'
    )

    monkeypatch.delenv("MNGR_ALLOW_UNKNOWN_CONFIG", raising=False)
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    cmd = _make_strict_test_command()

    result = cli_runner.invoke(cmd, [], obj=plugin_manager, catch_exceptions=True)

    # ConfigParseError extends ClickException, which click catches and renders to
    # the runner's output before exiting non-zero. Check the rendered output rather
    # than result.exception, which becomes SystemExit(1) after click's handler runs.
    assert result.exit_code != 0
    assert "bogus_typo_param" in result.output


@pytest.mark.allow_warnings(match=r"Unknown parameter 'bogus_typo_param'")
def test_setup_command_context_warns_on_unknown_command_param_when_lax(
    cli_runner: CliRunner,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
    log_warnings: list[str],
) -> None:
    """With MNGR_ALLOW_UNKNOWN_CONFIG=1, a typo in [commands.create] should warn, not raise."""
    (tmp_path / "settings.toml").write_text(
        'is_allowed_in_pytest = true\n\n[commands.create]\nbogus_typo_param = "x"\n'
    )

    monkeypatch.setenv("MNGR_ALLOW_UNKNOWN_CONFIG", "1")
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path))

    cmd = _make_strict_test_command()

    result = cli_runner.invoke(cmd, [], obj=plugin_manager, catch_exceptions=False)

    assert result.exit_code == 0, f"output={result.output!r} exception={result.exception!r}"
    assert any("bogus_typo_param" in msg for msg in log_warnings), (
        f"Expected a warning mentioning the unknown param, got: {log_warnings}"
    )


# =============================================================================
# Tests for the narrowing guard on --setting overrides.
# =============================================================================


@pytest.mark.parametrize(
    "flag_setting",
    [
        "allow_settings_key_assignment_narrowing=true",
        "allow_settings_key_assignment_narrowing=false",
        # Hyphenated spelling normalizes to the same field.
        "allow-settings-key-assignment-narrowing=true",
    ],
)
def test_apply_settings_to_config_rejects_setting_the_narrowing_flag(flag_setting: str, mngr_test_prefix: str) -> None:
    """``--setting`` cannot set ``allow_settings_key_assignment_narrowing``: the
    narrowing guard runs while loading the settings files and env vars, before
    ``--setting`` is applied, so a ``--setting`` value would be misleading. It
    raises a clear error pointing to the settings file / env var instead.
    """
    config = MngrConfig(prefix=mngr_test_prefix)
    with pytest.raises(UserInputError, match="allow_settings_key_assignment_narrowing"):
        apply_settings_to_config(config, (flag_setting,), frozenset())
