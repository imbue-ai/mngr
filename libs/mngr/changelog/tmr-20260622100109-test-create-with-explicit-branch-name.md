- Fixed the `test_create_with_explicit_branch_name` e2e tutorial test (covering
  `mngr create --branch ":feature/my-task"`). It now carries a
  `@pytest.mark.timeout(120)` so its several slow `mngr` subprocess calls no
  longer trip the 10s default pytest timeout, and the inaccurate
  `@pytest.mark.rsync` mark was removed: a local git-worktree create from a
  clean repo never invokes rsync, so the resource guard correctly flagged the
  mark as never exercised.
