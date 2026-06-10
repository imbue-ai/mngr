Strengthened the e2e coverage for `mngr config get` on a missing key.

- `test_config_get_missing_key` now pins the exact exit code (1) instead of just "non-zero", and asserts the "Key not found" diagnostic stays on stderr with stdout empty, so a caller piping stdout into a variable never captures the error text as a value.

- Added `test_config_get_missing_key_json`, covering the machine-readable branch: `mngr config get <missing> --format json` exits 1 and emits a structured `{"error": ..., "key": ...}` document on stdout.
