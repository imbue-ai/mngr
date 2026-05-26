## libs/mngr

- Strengthened `test_create_help_succeeds` e2e test: now also asserts that stdout includes the command header (`mngr create`) and an `EXAMPLES` section, and that stderr is empty. Added a sibling `test_create_help_shorthand_succeeds` that exercises the `-h` shorthand and asserts it produces identical output to `--help`.
