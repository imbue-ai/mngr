Fix the `test_exec_short_form` tutorial e2e test (`mngr x my-task "git status"`).

The test was marked with `@pytest.mark.rsync`, but exec on a local command agent never invokes rsync, so the resource guard failed the otherwise-passing test ("Test marked with @pytest.mark.rsync but never invoked rsync"). Removed the superfluous mark; the test now passes. Release tests do not run in CI, so this stale blanket mark had gone unverified.
