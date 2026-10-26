"""Integration tests for the archive CLI command."""

import time
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.archive import archive
from imbue.mngr.utils.testing import create_test_agent_via_cli
from imbue.mngr.utils.testing import shadow_program_on_path
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr.utils.testing import tmux_session_exists


@pytest.mark.tmux
def test_archive_skips_agent_whose_state_is_unreadable_without_force(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent whose state cannot be read may be running, so archiving it needs --force like a running one."""
    agent_name = f"test-archive-unreadable-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        create_test_agent_via_cli(
            cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 120006"
        )

        with monkeypatch.context() as patch:
            shadow_program_on_path(patch, tmp_path / "shims", "tmux", 'echo "lost server" >&2\nexit 1')
            archive_result = cli_runner.invoke(archive, [agent_name], obj=plugin_manager, catch_exceptions=False)

        assert archive_result.exit_code == 0
        assert "No agents to archive" in archive_result.output
        assert tmux_session_exists(session_name)
