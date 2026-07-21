# e2e tutorial test fixes: `test_create_clone`

- Fixed: removed the spurious `@pytest.mark.rsync` mark from the `test_create_clone` e2e tutorial test. The command it exercises (`mngr create --transfer=git-mirror`) transfers the repo via git, not rsync, so the rsync resource guard failed the test on its NEVER_INVOKED check (matching sibling `test_create_copy`, which already documents omitting the mark for the same reason).

- Changed: dropped the redundant `.git`-directory check from `test_create_clone`. The test's documented scope is clone *fidelity* (separate work_dir, identical HEAD SHA, fresh per-agent branch); the `.git`-directory basics are already covered by sibling `test_create_copy`.
