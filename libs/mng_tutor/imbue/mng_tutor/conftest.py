"""Test fixtures for mng-tutor.

Uses shared plugin test fixtures from mng for common setup (plugin manager,
environment isolation, git repos, temp_mng_ctx, local_provider, etc.).
"""

from imbue.mng.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())
