Fixed the `test_gc_background_watch` e2e tutorial test, which was failing under the global 10-second pytest timeout: a single cold `mngr` invocation already exceeds that, so the test now carries an explicit `@pytest.mark.timeout(120)` like the other gc tutorial tests.

Strengthened the same test to verify the concrete effect of `mngr config set commands.destroy.gc false` by reading the written project `settings.toml` back (rather than only checking the command's exit code).
