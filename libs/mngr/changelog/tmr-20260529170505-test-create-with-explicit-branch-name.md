Added an explicit `@pytest.mark.timeout(120)` to the e2e tutorial test
`test_create_with_explicit_branch_name` so it no longer hits the default 10s
pytest timeout when run outside the offload harness. Also strengthened the test
to verify the explicitly-named branch is based off the current branch's commit.
