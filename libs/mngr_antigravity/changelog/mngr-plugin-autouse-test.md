Standardized this plugin's test setup on `register_plugin_test_fixtures(globals())`
instead of `pytest_plugins = ["imbue.mngr.conftest"]`, so HOME isolation is wired
the same single way across all mngr plugins. Internal test-infrastructure change
only; no user-facing behavior change.
