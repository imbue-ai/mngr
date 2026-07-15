Fixed the e2e tutorial test suite so `mngr list`-based tests no longer fail purely because the monorepo dev/test venv installs every provider plugin.

- The shared `e2e` fixture now sets `enabled_backends` to an explicit allowlist (`local`, `ssh`, `modal`, plus `docker` only for tests marked `docker`/`docker_sdk`) instead of leaving it empty (which enabled every installed backend). Backends such as AWS and Azure deliberately report themselves as *unavailable* (`mngr list` exit code 6) when their cloud credentials are absent, so without the allowlist any `mngr list` in the suite failed just because those unconfigured plugins happened to be installed.

- Removed the inappropriate `@pytest.mark.rsync` marker from `test_create_with_project_label`: the default local worktree create it exercises never invokes rsync, so the resource guard flagged the mark as superfluous once the test body started passing.
