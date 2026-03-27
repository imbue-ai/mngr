"""Test fixtures for mngr-tutor.

Uses shared plugin test fixtures from mngr for common setup (plugin manager,
environment isolation, git repos, temp_mngr_ctx, local_provider, etc.).
"""

from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())
