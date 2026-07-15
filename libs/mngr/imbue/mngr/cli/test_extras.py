"""Acceptance tests for the mngr extras command against the real environment."""

import pytest
from click.testing import CliRunner

from imbue.mngr.cli.extras import extras


@pytest.mark.acceptance
@pytest.mark.timeout(60)
def test_extras_status_probes_real_claude_cli(cli_runner: CliRunner) -> None:
    """`mngr extras` status works against the real environment, with no stubs.

    The unit tests in extras_test.py deliberately replace the ``claude`` CLI
    with a fast stub, because the real one is a Node process whose startup on
    a contended sandbox can cross the global 10s offload pytest-timeout. This
    acceptance test keeps the real shell-out covered (when claude is
    installed, the status probe runs ``claude plugin list --json`` for real)
    under a timeout sized for Node startup.
    """
    result = cli_runner.invoke(extras, [])
    assert result.exit_code == 0
    assert "Extras" in result.output
    # The claude-plugin line renders in both environments: "claude not
    # installed" without claude, per-plugin statuses with it.
    assert "claude-plugin" in result.output
