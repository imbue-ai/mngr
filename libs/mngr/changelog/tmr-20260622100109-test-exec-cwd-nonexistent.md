Fixed the `test_exec_cwd_nonexistent` release test (covering the `mngr exec --cwd` tutorial block) so it no longer fails on a stale `@pytest.mark.rsync` resource-guard mark. A local `--type command` agent uses git-worktree mode and never invokes rsync, so the mark was superfluous and the resource guard reported it as never-invoked.

Strengthened the same test to assert that exec's error output references the missing directory, tying the nonzero exit to the bad `--cwd` rather than accepting any unrelated failure.
