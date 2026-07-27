Speed up the `build-minds-snapshot` -> `test-minds-snapshot` CI chain (the PR critical path) (MIND-141):

- Scope the `minds_snapshot_resume` suite's offload discovery to just `apps/minds/test_snapshot_resume.py` and `apps/minds/test_sync_e2e.py`. Local `pytest --collect-only` on the CI runner drops from ~90s (full-monorepo collection) to ~50s cold (collection itself is under a second; the remainder is fixed interpreter/plugin startup); the tests themselves are unchanged.

- Make the snapshot image's third-party dependency installs (uv + pnpm) cacheable across CI runs by staging them in manifests-only Modal layers, so source-only commits skip ~70s of reinstall in `build-minds-snapshot`.

See `dev/changelog/mngr-mind-141-minds-snapshot-ci-speed.md` for details and the new guard tests.
