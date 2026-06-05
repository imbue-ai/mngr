"""Unit tests for the git push/pull CLI subcommand group."""

import pytest
from click.testing import CliRunner

from imbue.mngr.cli.git import _resolve_remote_endpoint
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import UserInputError
from imbue.mngr.main import cli
from imbue.mngr.primitives import HostLocationAddress


def test_git_push_help_describes_passthrough() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["git", "push", "--help"])
    assert result.exit_code == 0
    # The passthrough feature is surfaced via the GIT_ARGS metavar (git.py:94).
    assert "GIT_ARGS" in result.output


def test_git_pull_help_describes_passthrough() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["git", "pull", "--help"])
    assert result.exit_code == 0
    # The passthrough feature is surfaced via the GIT_ARGS metavar (git.py:131).
    assert "GIT_ARGS" in result.output


def test_resolve_remote_endpoint_rejects_bare_local_path(temp_mngr_ctx: MngrContext) -> None:
    """A bare local path (no agent, no host) is rejected by ``_resolve_remote_endpoint``.

    See git.py:67-71: the resolver raises ``UserInputError`` directing the user
    to plain ``git push``/``git pull`` for local-only operations.
    """
    bare_local = HostLocationAddress(agent=None, host=None, path=None)
    with pytest.raises(
        UserInputError,
        match=r"git push/pull requires an agent or remote host",
    ):
        _resolve_remote_endpoint(bare_local, temp_mngr_ctx, is_start_desired=False)
