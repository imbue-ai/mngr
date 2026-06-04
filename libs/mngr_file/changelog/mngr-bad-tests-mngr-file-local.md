Improved the quality of the mngr-file test suite (test-only changes, no behavior change):

- Added a real end-to-end test that drives `mngr file put` and `mngr file get` through the CLI and asserts the content round-trips, replacing a test that bypassed the command code and only exercised the underlying host interface.
- Strengthened the localhost listing test to create a known file and directory and assert they appear with the correct type and size, instead of merely asserting the listing was non-empty.
- Replaced tautological `FileEntry` constructor assertions with tests that verify immutability and that optional fields default to `None` when omitted.
- Pinned the full `PathRelativeTo` and `FileType` serialized-value mappings via snapshots so any rename/add/remove is caught.
- Tightened the CLI argument-validation tests to assert a usage error (exit code 2 with a "Missing argument" message) rather than just a non-zero exit code.
- Parametrized the three near-identical `_is_volume_accessible_path` tests into one.
