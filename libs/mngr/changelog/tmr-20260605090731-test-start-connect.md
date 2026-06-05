Fixed the `test_start_connect` e2e tutorial test (covers `mngr start <agent> --connect`):
raised its per-test timeout so the full local create + start round-trip fits, and
dropped the inapplicable `@pytest.mark.modal` mark (starting a named local agent never
enumerates Modal). Also strengthened the test to verify the `--connect` path actually
ran the connect command (rather than only checking the command's exit code).
