Marked two more Modal acceptance tests `@pytest.mark.flaky`, so offload retries them instead of failing the run.

`test_exec_echo_on_modal` and `test_mngr_create_transfers_git_repo_with_untracked_files` intermittently lose the fresh-sandbox sshd boot race -- paramiko reporting `Error reading SSH protocol banner`, then an `EOFError` during the initial-snapshot write -- which outlasts mngr's deliberately bounded banner retry. This is the same failure mode their already-marked neighbours were marked for; these two were simply missed, and each was observed failing on one CI run and passing on the next with no change to the code under test.

The marker only buys a retry. The underlying race is unchanged and still worth fixing at the source.
