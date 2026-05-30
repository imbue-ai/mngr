Fixed the `test_create_modal_retry` release e2e test, which had regressed after
`mngr create` lost its source-coded default agent type. The test now passes an
explicit `--type command` (matching the convention in the other create e2e
tests) and additionally verifies the created agent appears in `mngr list` on a
Modal host.
