Fixed the `test_troubleshoot_gc_dry_run_then_gc` release test so it no longer
trips the default 10s pytest timeout: `mngr gc` walks every configured provider
(including Modal), which routinely takes longer than 10s. The test now carries an
explicit `@pytest.mark.timeout(120)` and gives each `mngr gc` invocation a
generous per-run timeout, matching the pattern used by the sibling
`test_troubleshoot_destroy_and_recreate_modal` test.
