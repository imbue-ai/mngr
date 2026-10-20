`test_clone_git_repo_checks_out_working_tree` is marked flaky, like its sibling clone tests: under a loaded parallel run its local `git clone` can exceed the 10 s per-test budget.
