"""Release tests for the check_deps (mngr dependencies) command.

These tests actually invoke package managers to install missing dependencies,
so they are slow and require network access.
"""

import pytest
from click.testing import CliRunner

from imbue.mngr.cli.check_deps import check_deps


@pytest.mark.release
@pytest.mark.timeout(120)
def test_check_deps_install_auto(cli_runner: CliRunner) -> None:
    """Verify `mngr dependencies --install auto` executes the real check/install flow to completion.

    Asserts the "System dependencies" section header appears in the output, which is only emitted
    once the command has actually walked its dependency checks. This fails if the flow short-circuits
    or never reaches the dependency reporting stage.
    """
    result = cli_runner.invoke(check_deps, ["--install", "auto"])
    assert "System dependencies" in result.output
