"""Integration tests for pair plugin loading via setuptools entry points.

These tests verify that the mng-pair plugin is properly discovered and
registered when installed in the same environment.
"""

from imbue.mng.main import PLUGIN_COMMANDS


def test_pair_command_is_registered_via_entry_points() -> None:
    """Verify that the pair command is discovered via entry points.

    The mng_pair package registers the 'pair' command via a setuptools entry
    point. This test verifies that the plugin is discovered and the command
    is available after loading.
    """
    plugin_command_names = [cmd.name for cmd in PLUGIN_COMMANDS]
    assert "pair" in plugin_command_names
