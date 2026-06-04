Strengthened the resource-guards test suite after a test-quality review:

- The two `generate_*_wrapper_script` unit tests now assert the full generated
  bash via `inline-snapshot` instead of scattered substring checks, so a
  branch swap, a dropped tracking-file touch, or a corrupted exec/delegate line
  is caught.
- Added two end-to-end pytester tests that actually run the stub wrapper for a
  guarded-but-absent binary, covering its block (records the missing mark) and
  allow (tracks the call despite exiting 127) paths, which previously had no
  behavioral coverage.
- `register_sdk_guard` registration tests now assert through the public
  `get_guarded_resource_names()` and behavioral install-counting rather than
  indexing private module state.
- Removed a misleading nested `pytest.raises(StopIteration)` and clarified the
  intent of several deferred-check tests.
