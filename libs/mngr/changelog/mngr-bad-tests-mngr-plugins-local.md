Improved the plugin test suite under `imbue/mngr/plugins/` (test-only changes, no user-visible behavior change):

- Plugin CLI-command tests now drive the production `_register_plugin_commands()` wiring against the real `cli` group instead of re-implementing the registration loop in the test, so a regression in that function would actually be caught.
- Replaced the module-global `_captured_values` dict with per-test capture dicts, removing shared state between tests.
- "No commands added" tests now assert the real invariant (the cli command set is unchanged) rather than the absence of an unrelated command name.
- Deleted a test that only exercised a decorator defined inside the test file (`with_plugin_cli_options`), since the underlying `apply_plugin_cli_options` is already covered directly.
- Hoisted the duplicated "install a plugin manager and restore it" dance and the `LifecycleTracker` plugin into a shared `imbue/mngr/plugins/testing.py`, and moved the `lifecycle_tracker` fixture into `imbue/mngr/plugins/conftest.py`.
- Catalog-iterating isolation tests now guard against vacuous success when a tier or the catalog becomes empty, and their stale "BASIC"/"EXTRA" tier naming was updated to "INDEPENDENT"/"DEPENDENT".
- The plugin help-topics "returning no topics is harmless" test now asserts the topic registry is unchanged instead of only checking the exit code.
- Added clarifying comments to the `is_blocked` tripwire tests noting they intentionally re-assert the fixture's blocking configuration.
