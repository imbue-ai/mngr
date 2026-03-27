"""Integration tests for schedule plugin loading via setuptools entry points."""

from imbue.mng.main import PLUGIN_COMMANDS


def test_schedule_command_is_registered_via_entry_points() -> None:
    """Verify that the schedule command is discovered via entry points."""
    plugin_command_names = [cmd.name for cmd in PLUGIN_COMMANDS]
    assert "schedule" in plugin_command_names
