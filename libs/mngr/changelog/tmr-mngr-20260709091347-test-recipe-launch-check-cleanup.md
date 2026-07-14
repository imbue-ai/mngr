Fixed the `test_recipe_launch_check_cleanup` e2e release test:

- Scoped its two `mngr list` invocations to the local provider (`mngr list --running --provider local` and `mngr list --provider local`), matching the local command-agent stand-in. A bare `mngr list` also queries remote provider backends (e.g. aws, docker) that are enabled by default but unreachable in the isolated e2e profile (no cloud credentials, no docker daemon), so it exited non-zero for reasons unrelated to the launch->check->cleanup recipe under test.

- Removed the stale `@pytest.mark.rsync` mark. The local command-agent stand-in never invokes rsync (only the tutorial's modal agent would sync the repo to a remote host), so the resource guard correctly flagged the mark as unused.
