Fixed the release e2e test `test_create_command_agent_runs_post_dash_command_in_agent` so its verification step scopes `mngr list` to the local provider (`--provider local`), where the command agent under test actually runs.

Previously the test ran an unscoped `mngr list --format json`, which fans out across every enabled provider with the default `--on-error abort`. In a checkout that has the `mngr_aws` backend plugin installed but no AWS credentials, that aborts with exit code 1 ("Discovery failed for provider 'aws'") even though the local agent was created and running correctly, so the test failed on an unrelated cloud-provider credential issue.

Also dropped the now-spurious `@pytest.mark.rsync` mark: a local create in a git repo uses the `git-worktree` transfer mode, so this test never invokes rsync, and the resource guard correctly flagged the unused mark. Strengthened the assertions to also check the agent's `type` is `command`.
