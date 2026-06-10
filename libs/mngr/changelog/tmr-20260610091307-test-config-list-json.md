Strengthened the `test_config_list_json` e2e tutorial test for `mngr config
list --format json`. In addition to verifying the persisted `headless` value
round-trips into the merged JSON document, it now asserts the document carries
only the top-level `config` object (no `scope`/`path` keys), confirming that
`config list` without a scope returns the merged view rather than a single
scope's file.
