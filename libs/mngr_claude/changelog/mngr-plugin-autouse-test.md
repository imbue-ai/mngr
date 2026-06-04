Standardized mngr_claude's test setup on `register_plugin_test_fixtures(globals())`
for HOME isolation (matching every other mngr plugin), keeping
`pytest_plugins = ["imbue.mngr_modal.conftest"]` only to share mngr_modal's test
fixtures (`modal_subprocess_env`, etc.). Removed the now-redundant
`enabled_plugins` override. Internal test-infrastructure change only; no
user-facing behavior change.
