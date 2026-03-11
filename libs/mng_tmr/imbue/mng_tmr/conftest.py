"""Test fixtures for mng-test-mapreduce.

Uses shared plugin test fixtures from mng for common setup (plugin manager,
environment isolation, git repos, etc.) and defines test-mapreduce-specific fixtures below.
"""

from imbue.mng.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())
