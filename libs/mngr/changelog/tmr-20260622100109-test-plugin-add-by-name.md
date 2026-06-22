- Strengthened the `test_plugin_add_by_name` e2e tutorial test: it now asserts
  the command aborts with exit code 1 (ruling out a click usage error), that
  `my-plugin` is accepted as the source argument (no "No such option"), and that
  the abort happens for the documented reason (mngr not installed via `uv tool
  install`). It also now uses the `expect()` assertion style for consistency
  with the other plugin tests.
