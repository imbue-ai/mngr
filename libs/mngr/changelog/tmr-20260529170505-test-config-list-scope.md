Fixed the `test_config_list_scope` e2e tutorial test, which was failing under the
default 10s per-test timeout because it runs three sequential `mngr config list`
subprocesses (each with several seconds of cold CLI startup). Added a
`@pytest.mark.timeout(60)` override, matching the convention used by other e2e
tutorial tests. Also strengthened the test to verify scope-specific output
(each `--scope` lists only its own config file) rather than only checking exit codes.
