Standardized mngr_modal's project conftest on `register_plugin_test_fixtures(globals())`
for HOME isolation, the same single mechanism used by every mngr plugin. The
Modal-specific fixtures (including the credential-loading `setup_test_mngr_env`
override) are unchanged. Internal test-infrastructure change only; no user-facing
behavior change.
