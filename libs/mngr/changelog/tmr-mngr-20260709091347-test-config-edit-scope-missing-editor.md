Fixed the `test_config_edit_scope_missing_editor` release test so it no longer fails with a spurious 10s func-only timeout: a single `mngr` invocation exceeds the default timeout because of startup cost, so the test now carries `@pytest.mark.timeout(60)` like its sibling config tests.

Tightened the same test to also assert that the missing-editor failure surfaces the actionable "Editor not found" message rather than a bare Python traceback, matching the documented scope.
