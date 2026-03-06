"""Unit tests for the kanpan CLI command."""

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng_kanpan.cli import KanpanCliOptions
from imbue.mng_kanpan.cli import kanpan


def test_kanpan_cli_options_can_be_instantiated() -> None:
    opts = KanpanCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.output_format == "human"


def test_kanpan_command_calls_run_kanpan(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kanpan command should call run_kanpan with the MngContext."""
    called_with = []
    monkeypatch.setattr(
        "imbue.mng_kanpan.cli.run_kanpan",
        lambda mng_ctx: called_with.append(mng_ctx),
    )
    result = cli_runner.invoke(kanpan, [], obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0
    assert len(called_with) == 1
