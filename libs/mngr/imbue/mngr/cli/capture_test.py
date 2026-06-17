"""Unit tests for the capture CLI command."""

import shlex
import subprocess
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.capture import capture
from imbue.mngr.cli.create import create
from imbue.mngr.hosts.tmux import TmuxSessionTarget
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import capture_tmux_pane_contents
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
    temp_work_dir: Path,
    mngr_test_prefix: str,
) -> None:
    """Capture command should output the visible pane content for a running agent."""
    agent_name = "test-capture-visible"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "echo CAPTURE_TEST_MARKER && sleep 493827",
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
            lambda: "CAPTURE_TEST_MARKER" in capture_tmux_pane_contents(TmuxWindowTarget(session_name=session_name)),
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


@pytest.mark.tmux
def test_capture_specific_window(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    temp_work_dir: Path,
    mngr_test_prefix: str,
) -> None:
    """--window should capture a non-primary tmux window in the agent's session."""
    agent_name = "test-capture-window"
    session_name = f"{mngr_test_prefix}{agent_name}"
    extra_window_name = "captureprobe"
    extra_window_marker = "EXTRA_WINDOW_MARKER"

    with tmux_session_cleanup(session_name):
        create_result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--type",
                "command",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                "--",
                "echo CAPTURE_TEST_MARKER && sleep 493827",
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

        # Open an extra named window running its own marker command. mngr already
        # creates a "terminal" window alongside the agent, so target by name rather
        # than assuming a particular index.
        session_target = TmuxSessionTarget(session_name=session_name)
        subprocess.run(
            shlex.split(
                f"tmux new-window -t {session_target.as_shell_arg()} -n {extra_window_name} "
                f"-d 'echo {extra_window_marker}; sleep 493827'"
            ),
            check=True,
        )

        wait_for(
            lambda: extra_window_marker
            in capture_tmux_pane_contents(TmuxWindowTarget(session_name=session_name, window=extra_window_name)),
            timeout=5.0,
            error_message="Extra window output did not appear in tmux pane",
        )

        result = cli_runner.invoke(
            capture,
            [agent_name, "--window", extra_window_name],
            obj=plugin_manager,
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert extra_window_marker in result.output
        assert "CAPTURE_TEST_MARKER" not in result.output
