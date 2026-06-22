Fixed the `test_create_with_env_file` e2e tutorial test, which was failing under
the default 10s per-test timeout: a real `mngr create` (host setup) takes longer
than that. Added a `@pytest.mark.timeout(120)` override, mirroring the sibling
create-based tests in the same file. Test-only; no user-visible behavior change.


Strengthened the same test to also confirm the `--env-file` variable reaches the
running agent's runtime environment via `mngr exec my-task 'printenv FOO'`, not
just the on-disk env file, mirroring the sibling `--env` happy-path test.
