"""Test fixtures for mngr-mapreduce.

Uses shared plugin test fixtures from mngr for common setup (plugin manager,
environment isolation, git repos, etc.).
"""

from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())
