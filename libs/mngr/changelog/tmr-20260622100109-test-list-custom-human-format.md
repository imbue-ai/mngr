Hardened the `mngr list --format '{name} ({state})'` tutorial e2e test
(`test_list_custom_human_format`) so it no longer depends on which providers happen
to be reachable in the environment. The bare `mngr list` fans out to every enabled
backend, so its exit code was tied to whether unrelated backends (AWS, Docker, ...)
were reachable -- which differs between CI (cloud credentials present, Docker
running) and a developer machine. The test now scopes discovery to the local
provider, where the agent it creates actually lives, while leaving the tutorial
block verbatim. Also dropped the test's spurious `@pytest.mark.rsync` mark: a local
command agent created from a git repo transfers via `git-worktree` (a pure git
operation) and never invokes rsync, so the resource guard flagged the unused mark.
Test-only change; no user-visible behavior change.
