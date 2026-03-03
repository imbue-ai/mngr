"""Integration tests for the clone CLI command."""

from pathlib import Path
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.clone import clone
from imbue.mng.cli.list import list_command
from imbue.mng.utils.testing import create_test_agent_via_cli
from imbue.mng.utils.testing import tmux_session_cleanup
from imbue.mng.utils.testing import tmux_session_exists


@pytest.mark.tmux
def test_clone_creates_agent_from_source(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that clone creates a new agent by delegating to create --from-agent."""
    source_name = f"test-clone-source-{uuid4().hex}"
    clone_name = f"test-clone-target-{uuid4().hex}"
    source_session = f"{mng_test_prefix}{source_name}"
    clone_session = f"{mng_test_prefix}{clone_name}"

    with tmux_session_cleanup(source_session), tmux_session_cleanup(clone_session):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, source_name)

        # Clone the source agent with a positional name (the primary documented usage pattern)
        clone_result = cli_runner.invoke(
            clone,
            [
                source_name,
                clone_name,
                "--agent-cmd",
                "sleep 482917",
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert clone_result.exit_code == 0, f"Clone failed with: {clone_result.output}"
        assert tmux_session_exists(clone_session), f"Expected clone session {clone_session} to exist"

        # Verify both agents appear in list output
        list_result = cli_runner.invoke(
            list_command,
            [],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert list_result.exit_code == 0
        assert source_name in list_result.output, f"Expected source agent in list output: {list_result.output}"
        assert clone_name in list_result.output, f"Expected clone agent in list output: {list_result.output}"
