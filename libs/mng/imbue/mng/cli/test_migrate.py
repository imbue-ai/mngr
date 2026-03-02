"""Integration tests for the migrate CLI command."""

from pathlib import Path
from uuid import uuid4

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.list import list_command
from imbue.mng.cli.migrate import migrate
from imbue.mng.utils.testing import create_test_agent_via_cli
from imbue.mng.utils.testing import tmux_session_cleanup
from imbue.mng.utils.testing import tmux_session_exists


@pytest.mark.tmux
def test_migrate_clones_and_destroys_source(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that migrate creates a new agent and destroys the source."""
    source_name = f"test-migrate-source-{uuid4().hex}"
    target_name = f"test-migrate-target-{uuid4().hex}"
    source_session = f"{mng_test_prefix}{source_name}"
    target_session = f"{mng_test_prefix}{target_name}"

    with tmux_session_cleanup(source_session), tmux_session_cleanup(target_session):
        create_test_agent_via_cli(cli_runner, temp_work_dir, mng_test_prefix, plugin_manager, source_name)

        # Migrate the source agent to a new name
        migrate_result = cli_runner.invoke(
            migrate,
            [
                source_name,
                target_name,
                "--agent-cmd",
                "sleep 482917",
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert migrate_result.exit_code == 0, f"Migrate failed with: {migrate_result.output}"

        # Verify the target agent exists
        assert tmux_session_exists(target_session), f"Expected target session {target_session} to exist"

        # Verify the source agent was destroyed
        assert not tmux_session_exists(source_session), f"Expected source session {source_session} to be destroyed"

        # Verify via list: target should be present, source should not
        list_result = cli_runner.invoke(
            list_command,
            [],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert list_result.exit_code == 0
        assert target_name in list_result.output, f"Expected target agent in list output: {list_result.output}"
        assert source_name not in list_result.output, f"Expected source agent NOT in list output: {list_result.output}"
