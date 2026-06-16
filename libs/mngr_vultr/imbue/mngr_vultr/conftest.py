"""Test fixtures for mngr_vultr.

Uses shared plugin test fixtures from mngr to avoid duplicating common
fixture code across plugin libraries (mirrors mngr_schedule's conftest).
"""

from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())
