Strengthened the `test_plugin_add_by_name` e2e tutorial test to assert on the
actual `uv tool install` guard error message instead of only checking for a
non-zero exit code, and corrected its misleading comment about registry lookups.
