"""Test fixtures for mngr-usage.

Uses shared plugin test fixtures from mngr (plugin manager, environment isolation,
temp_mngr_ctx, local_host, etc.).
"""

from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())
