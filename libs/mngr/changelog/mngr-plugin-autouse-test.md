Test-infrastructure cleanup: the shared mngr plugin test fixtures (HOME
isolation via the autouse `setup_test_mngr_env`, temp host/profile/config dirs,
git-repo helpers, and the shell-stub fixtures `stub_mngr_log_sh` /
`mngr_transcript_lib_sh`) are now single-sourced in
`imbue.mngr.utils.plugin_testing` and exposed through
`register_plugin_test_fixtures`. mngr's own `conftest.py` now registers that
shared set rather than redefining ~19 duplicate fixtures, keeping only the
fixtures that intentionally differ for mngr-core (the blocking `plugin_manager`,
`mngr_test_id`'s tmux-cleanup tracking, and `temp_mngr_ctx`'s provider-cache
reset). No user-facing behavior change.
