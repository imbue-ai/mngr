Split the mngr and minds release test suites cleanly.

Renamed the two jobs in `.github/workflows/release-tests.yml` to make it explicit they cover the *mngr* release suite: `test-docker-release` -> `test-mngr-release-docker` and `test-release` -> `test-mngr-release`. Both jobs now exclude the whole `apps/minds` tree by path (`--ignore apps/minds`) instead of enumerating minds markers, so the mngr release run never touches minds tests.

Folded all minds `@release` tests into the minds release job (`test-minds-release` in `ci.yml`): in addition to the `minds_deployment` group, it now installs Chromium and runs the plain minds `@release` tests (`-m 'release and not minds_deployment and not minds_services and not minds_snapshot_resume'`). This keeps the two release procedures separate (mngr = `v*` tag / dispatch; minds = manual `run_minds_release_tests` dispatch). Updated the stale `test-docker-release` reference in `offload-modal-release.toml`.

Bumped the pinned Claude Code CLI version from `2.1.141` to `2.1.160` (matching forever-claude-template) in the release-tests workflow, the `tmr-setup` action, and the minds snapshot build script, so they stay aligned with the release Dockerfile pin.
