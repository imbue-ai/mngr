"""Unit tests for the ``mngr tmr-specs`` CLI wrapper.

The bulk of the CLI logic lives in ``imbue.mngr_mapreduce.cli`` and the
``--`` separator class in ``imbue.mngr_tmr.cli``; both are tested there.
This file covers the spec-recipe glue: the option surface and the
test-roots default resolution.
"""

from pathlib import Path

from click.testing import CliRunner

from imbue.mngr_tmr.spec_cli import effective_test_roots
from imbue.mngr_tmr.spec_cli import tmr_specs


def test_spec_cli_help_contains_recipe_options(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(tmr_specs, ["--help"])
    assert result.exit_code == 0
    assert "--root" in result.output
    assert "--tests" in result.output
    assert "--area" in result.output
    assert "--tag" in result.output
    assert "--unit" in result.output
    assert "--name" in result.output
    assert "--mapper-prompt" in result.output
    assert "--reducer-prompt" in result.output


def test_spec_cli_help_contains_framework_options(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(tmr_specs, ["--help"])
    assert "--provider" in result.output
    assert "--timeout" in result.output
    assert "--reducer-timeout" in result.output
    assert "--max-parallel-agents" in result.output
    assert "--reintegrate" in result.output


def test_spec_cli_requires_the_corpus_root(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(tmr_specs, [])
    assert result.exit_code != 0
    assert "--root" in result.output


def test_effective_test_roots_defaults_to_the_corpus_roots_parent() -> None:
    assert effective_test_roots(Path("apps/minds/specs"), ()) == (Path("apps/minds"),)


def test_effective_test_roots_keeps_explicit_roots() -> None:
    explicit = (Path("apps/minds"), Path("libs/somelib"))
    assert effective_test_roots(Path("apps/minds/specs"), explicit) == explicit
