Fixed and tightened the `test_create_with_explicit_project` e2e tutorial test:

- Added `@pytest.mark.timeout(120)` (it previously hit the 10s default and timed out) and dropped the `modal`/`rsync` resource marks, which the local `--no-connect` command-agent create never exercises.

- The test now verifies the documented scope end-to-end: after the create, it reads `mngr list --provider local --format json` and asserts the new agent's `labels.project` is `other-project`, confirming `--project` overrides the directory-derived default rather than only checking that the command exited 0.
