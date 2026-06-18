"""Shared fixtures for mngr_vps tests.

Registers the standard plugin-test fixtures (including ``temp_mngr_ctx``,
``temp_host_dir``, ``plugin_manager``) provided by mngr's shared helper so
unit tests can build real ``MngrContext`` / provider instances without
booting the full mngr CLI surface.
"""

from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())
