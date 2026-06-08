Fixed a batch of breakage in the test suite introduced by a bulk merge:

- Added missing imports and a missing fixture parameter in the e2e tutorial tests (`json`, `re`, `Path`, `Any`, `sys`, and the `temp_git_repo` fixture) that caused F821 errors.
- Fixed a stale `_create_my_task` call signature, removed an unused `json` import, deduplicated stray imports, and deleted a dead duplicate transcript-staging helper in `test_transcript.py`.
- Applied ruff import-sorting (`destroy_test.py`, `start_test.py`) and formatting (14 e2e test files) that the merge left unformatted.
- Regenerated the CLI markdown docs to reflect new options the merge added (`mngr connect --connect-command`, `mngr destroy --dry-run`, and updates to `start`/`stop`/`message`/`snapshot`).
- Rephrased a comment in `test_templates.py` that coincidentally tripped the `exec()` ratchet (the prose read "exec (which ...").
- Replaced `@pytest.mark.flaky` with `@pytest.mark.timeout(30)` on `test_list_command_with_sort_by_name`, the one sibling in the `test_list_command_with_*` family missed by the earlier timeout-flake audit (its teardown latency trips the 10s default under CI load; reruns do not help latency).

No user-facing behavior change.
