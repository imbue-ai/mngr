"""Pytest fixtures for ``mngr-latchkey`` unit tests.

Reuses the shared ``register_plugin_test_fixtures`` helper from
``imbue-mngr`` so the per-test ``temp_mngr_ctx`` / ``temp_host_dir`` /
``local_provider`` fixtures match the conventions of every other mngr
plugin and we don't reinvent them locally.
"""

from imbue.mngr.utils.plugin_testing import register_plugin_test_fixtures

register_plugin_test_fixtures(globals())
