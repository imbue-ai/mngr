Improved test quality under `scripts/`:

- Rewrote `scripts/josh/coordinator_test.py`: flattened the `Test*` classes into descriptively-named module-level test functions; replaced tests that no longer exercised production code (a handler test that only ran `echo` via the shell, an "update detection" test that only round-tripped JSON, and an initial-sync test that reimplemented `process_tasks`) with tests that drive the real `ProcessManager.spawn_handler` and `process_tasks` paths; and gave the long-lived handler-termination test a unique sleep duration.
- `scripts/open_issue.py` now accepts an injectable `open_url` callable instead of the test replacing `webbrowser.open` at runtime; the test asserts the title and body land in the correct URL query parameters (catching a title/body swap or missing encoding).
- Tightened `scripts/sync_common_ratchets_test.py` to cross-check discovered `test_ratchets.py` files against the independent project discovery instead of asserting hand-guessed `>= N` floors.
- Documented why `scripts/version_sync_test.py::test_package_graph_matches_pyproject_files` asserts by raising.
