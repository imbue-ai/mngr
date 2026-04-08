"""Release tests for the check_deps (mngr dependencies) command.

These tests actually invoke package managers to install missing dependencies,
so they are slow and require network access.
"""

import pytest
from click.testing import CliRunner

from imbue.mngr.cli.check_deps import check_deps


@pytest.mark.release
@pytest.mark.timeout(120)
def test_check_deps_all_flag(cli_runner: CliRunner) -> None:
    """Running 'mngr dependencies --all' runs the full check/install flow."""
    result = cli_runner.invoke(check_deps, ["--all"])
    assert "System dependencies" in result.output
