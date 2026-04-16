"""Unit tests for the capture CLI command."""

from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.capture import capture
from imbue.mngr.cli.create import create
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import capture_tmux_pane_contents
from imbue.mngr.utils.testing import register_test_agent_type
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr.utils.testing import tmux_session_exists


def test_capture_no_agent_headless_fails(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Capture with no agent in headless mode should fail with a clear error."""
    result = cli_runner.invoke(
        capture,
        ["--headless"],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "No agent specified" in result.output


@pytest.mark.tmux
def test_capture_outputs_pane_content(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_host_dir: Path,
    temp_work_dir: Path,
    mngr_test_prefix: str,
) -> None:
    """Capture command should output the visible pane content for a running agent."""
    register_test_agent_type(temp_host_dir, "test_capture_echo", "echo CAPTURE_TEST_MARKER && sleep 493827")
    agent_name = "test-capture-visible"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "test_capture_echo",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert create_result.exit_code == 0, f"Create failed: {create_result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=15.0,
            error_message=f"Expected session {session_name} to exist",
        )

        wait_for(
            lambda: "CAPTURE_TEST_MARKER" in capture_tmux_pane_contents(session_name),
            timeout=5.0,
            error_message="Echo output did not appear in tmux pane",
        )

        result = cli_runner.invoke(
            capture,
            [agent_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "CAPTURE_TEST_MARKER" in result.output
