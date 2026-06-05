Test-infrastructure cleanup: the shared mngr plugin test fixtures (HOME
isolation via the autouse `setup_test_mngr_env`, temp host/profile/config dirs,
git-repo helpers, and the shell-stub fixtures `stub_mngr_log_sh` /
`mngr_transcript_lib_sh`) are now single-sourced in
`imbue.mngr.utils.plugin_testing` and exposed through
`register_plugin_test_fixtures`. mngr's own `conftest.py` now registers that
shared set rather than redefining ~20 duplicate fixtures, keeping only the two
fixtures that intentionally differ for mngr-core: the blocking `plugin_manager`
and `mngr_test_id` (whose `worker_test_ids` tracking is consumed only by
mngr-core's `session_cleanup` leak scan, which plugins do not have). The shared
`temp_mngr_ctx` now resets the provider-instance cache on teardown for plugins
too, so that behavior no longer diverges. No user-facing behavior change.
