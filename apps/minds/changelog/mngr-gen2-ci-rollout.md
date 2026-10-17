`test_latchkey_e2e` no longer fails with `Unknown provider backend: docker` in the release job: it resets the backend registry (which the autouse plugin-manager fixture had loaded in local-only mode) before building its in-process mngr context, so the singleton's own load registers the docker backend its provider block names.

Version bumped to 0.6.0 and `FALLBACK_BRANCH` pinned to `minds-v0.6.0`: the first release line that runs on gen-2 slice boxes (the bake-time guard pairs `minds-v0.6+` tags with gen-2 boxes and older tags with gen-1).

`test_workspace_stop_start` is no longer behind the `MINDS_STOP_START_RELEASE_TEST=1` opt-in: with the CI boxes on gen-2 the stop upload rides the S3 IPv4 pin at ~100 MB/s, so the full cycle takes minutes and the test runs in every release dispatch (its stop deadline is now 30 minutes instead of 3.5 hours).

`test_web_chrome`'s poolless loop now reveals the seeded tombstone through the overview's "Show N destroyed workspaces" toggle before asserting on it: the chrome has hidden destroyed workspaces by default since 2026-08-13, and the test had skipped in every CI run since (no Chromium on the runner), so the drift was never seen.

`test_workspace_docker_container_is_present_and_stopped` (snapshot-resume suite) is marked flaky: its "every workspace container is exited" reading races other tests in the same offload sandbox that start containers, and the batch order is not fixed.

`test_storage_cleanup_grant_cycle` is marked flaky and the quota-enforcement tests' HTTP timeout is 120 s: the storage recheck is a live Cloudflare round trip (object listing plus token policy rewrites) that exceeded 60 s once. `test_web_chrome`'s re-unlock check reveals the two tombstones through the same toggle before asserting on their decrypted names.
