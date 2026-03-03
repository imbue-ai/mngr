from pathlib import Path
from unittest.mock import patch

import pluggy
from click.testing import CliRunner

import imbue.mng.main
import imbue.mng_tip.plugin as tip_plugin
from imbue.mng.main import cli
from imbue.mng_tip.invocation_logger import get_tip_data_dir


class TestPluginRegistration:
    def test_plugin_is_registered(self, plugin_manager: pluggy.PluginManager) -> None:
        """The tip plugin should be auto-discovered via setuptools entry points."""
        assert plugin_manager.is_registered(tip_plugin)


class TestPluginHookIntegration:
    @patch("imbue.mng_tip.tip_display._kick_off_async_tip_generation")
    def test_logs_invocation_on_command(
        self,
        mock_gen: object,
        plugin_manager: pluggy.PluginManager,
        cli_runner: CliRunner,
        temp_host_dir: Path,
    ) -> None:
        """Running a command should create an invocation log entry."""
        imbue.mng.main._plugin_manager_container["pm"] = plugin_manager

        cli_runner.invoke(cli, ["list"])

        invocations_path = get_tip_data_dir() / "invocations.jsonl"
        assert invocations_path.exists()
        content = invocations_path.read_text().strip()
        assert '"command": "list"' in content
