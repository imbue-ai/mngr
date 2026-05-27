"""Unit tests for the ``mngr tmr`` CLI wrapper.

The bulk of the CLI logic (option parsing, emit helpers, modal-snapshot
disable) lives in ``imbue.mngr_mapreduce.cli`` and is tested there. This
file only covers the TMR-specific glue: ``_TmrCommand``'s ``--`` separator
trick and the help-text surface contract.
"""

from typing import Any

import click
from click.testing import CliRunner
from click.testing import Result

from imbue.mngr_tmr.cli import _TmrCommand
from imbue.mngr_tmr.cli import tmr


def test_cli_help(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(tmr, ["--help"])
    assert result.exit_code == 0
    assert "PYTEST_ARGS" in result.output


def test_cli_help_contains_options(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(tmr, ["--help"])
    assert "--agent-type" in result.output
    assert "--poll-interval" in result.output
    assert "--output-dir" in result.output
    assert "--source" in result.output


def test_cli_help_contains_provider_env_label_options(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(tmr, ["--help"])
    assert "--provider" in result.output
    assert "--env" in result.output
    assert "--label" in result.output


def test_cli_help_drops_removed_options(cli_runner: CliRunner) -> None:
    """The integrator-specific flags and --use-snapshot are gone.

    The integrator (reducer) follows --provider and the mapper agent-type;
    snapshot building is automatic when the provider supports it.
    """
    result = cli_runner.invoke(tmr, ["--help"])
    assert "--integrator-provider" not in result.output
    assert "--integrator-type" not in result.output
    assert "--integrator-template" not in result.output
    assert "--use-snapshot" not in result.output


def test_cli_help_contains_timeout_options(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(tmr, ["--help"])
    assert "--timeout" in result.output
    assert "--reducer-timeout" in result.output
    assert "--max-parallel-agents" in result.output


def _invoke_tmr_command(
    args: list[str],
) -> tuple[Result, dict[str, Any]]:
    """Invoke a dummy _TmrCommand with the given args and return (result, captured_params)."""
    captured: dict[str, Any] = {}

    @click.command(cls=_TmrCommand, context_settings={"ignore_unknown_options": True})
    @click.argument("pytest_args", nargs=-1, type=click.UNPROCESSED)
    @click.option("--provider", default="local")
    @click.pass_context
    def dummy_cmd(ctx: click.Context, **kwargs: object) -> None:
        captured.update(kwargs)

    runner = CliRunner()
    result = runner.invoke(dummy_cmd, args)
    return result, captured


def test_tmr_command_splits_on_double_dash() -> None:
    """_TmrCommand correctly captures args after -- as testing_flags."""
    result, captured = _invoke_tmr_command(["tests/e2e", "--", "-m", "release"])
    assert result.exit_code == 0
    assert captured["pytest_args"] == ("tests/e2e",)
    assert captured["testing_flags"] == ("-m", "release")


def test_tmr_command_no_separator() -> None:
    """Without --, all args go into pytest_args and testing_flags is empty."""
    result, captured = _invoke_tmr_command(["tests/e2e", "tests/unit"])
    assert result.exit_code == 0
    assert captured["pytest_args"] == ("tests/e2e", "tests/unit")
    assert captured["testing_flags"] == ()


def test_tmr_command_only_flags() -> None:
    """-- with nothing before it gives empty pytest_args."""
    result, captured = _invoke_tmr_command(["--", "-m", "release", "-v"])
    assert result.exit_code == 0
    assert captured["pytest_args"] == ()
    assert captured["testing_flags"] == ("-m", "release", "-v")


def test_tmr_command_separator_only() -> None:
    """Just -- gives empty args and empty flags."""
    result, captured = _invoke_tmr_command(["--"])
    assert result.exit_code == 0
    assert captured["pytest_args"] == ()
    assert captured["testing_flags"] == ()


def test_tmr_command_options_before_separator() -> None:
    """Known options before -- are parsed normally, not captured as args."""
    result, captured = _invoke_tmr_command(["--provider", "docker", "tests/", "--", "-m", "release"])
    assert result.exit_code == 0
    assert captured["pytest_args"] == ("tests/",)
    assert captured["testing_flags"] == ("-m", "release")
    assert captured["provider"] == "docker"
