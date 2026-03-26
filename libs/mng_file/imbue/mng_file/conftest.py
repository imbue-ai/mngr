"""Test fixtures for mng-file.

Uses shared plugin test fixtures from mng for common setup (plugin manager,
environment isolation, git repos, etc.) and defines file-specific fixtures below.
"""

from imbue.mng.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())
