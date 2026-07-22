Speed up the `build-minds-snapshot` -> `test-minds-snapshot` CI chain, the PR critical path (~10.5 min) (MIND-141).

Scope offload's `minds_snapshot_resume` test discovery (`offload-modal-minds-snapshot.toml`) to the two test files that carry the mark via `[framework].paths`, cutting the local `pytest --collect-only` on the CI runner from ~90s (full-monorepo collection to find 12 tests) to a few seconds. The group filters pin `-c pyproject.toml` (a discovery-only arg) so the scoped paths still yield repo-root-relative test ids -- without it pytest would pick `apps/minds` as rootdir and emit ids that do not resolve at execution time.

Add `scripts/snapshot_minds_e2e_state_test.py` with guard tests asserting the discovery paths stay in sync with the set of files that apply the `minds_snapshot_resume` mark (so a new snapshot-resume test file can't silently escape CI) and that every offload group keeps the `-c pyproject.toml` pinning.
