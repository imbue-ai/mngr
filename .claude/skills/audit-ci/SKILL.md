---
name: audit-ci
description: Audit recent CI runs for anomalies (warnings, uncached docker builds, flaky/slow tests, regressions).
---

Find anomalies in recent CI runs -- especially the kind a human won't notice without poring over the logs of *passing* jobs. Hard failures already get human attention; the value here is the subtler stuff: warnings, wasteful rebuilds, flaky tests that recovered on retry, slow steps, and failures quietly recurring across many PRs. Identify and report; don't fix. If you fan out with subagents, ensure they follow the guidelines below (they don't inherit this file), and verify load-bearing claims at the source yourself.

## Where results live (counterintuitive)

In `.github/workflows/ci.yml`, the release tests run only on `release`-branch pushes, so a normal PR/main audit never sees them.

**Test results are not in the workflow jobs.** Each test job ends by POSTing a *separate* check-run (the `report-flaky-aware-tests` action) named `Unit + Integration Tests` / `Acceptance Tests`. They show as `in 0s` with `/runs/<id>` URLs (the PR "Checks" format, not `/actions/runs/.../job/`). Fetch the retry/flaky table:
`gh api repos/imbue-ai/mngr/check-runs/<id> --jq '{name,conclusion,summary:.output.summary}'`
- Conclusion: `success` = clean; **`neutral` = passed only after retries (the flaky signal)**; `failure` = hard fail.
- Summary has a `| Test | Runs | Final | @flaky |` table; `Final` reads `flaked N, passed M` or `passed`. A high run-count with `Final = passed` is just offload's by-design reruns, not a flake.

## Getting the data

Start cheap: `gh run view <run_id>` lists jobs + durations and has an **ANNOTATIONS** section that surfaces warnings and failure messages without pulling logs. Drop to `gh run view --job <id> --log` only when annotations don't explain something.

## What to look for

1. **Warnings** (the ANNOTATIONS section, including on otherwise-passing runs).
2. **Uncached image rebuilds**: offload caches its base image in git notes (`refs/notes/offload-images`, ~48h TTL), so occasional misses are fine -- flag *frequent*/no-change misses, or a missing `contents: write` perm / failed `git fetch refs/notes/*` (defeats the cache every run). Grep logs for base-image build / `cache miss`.
3. **Flaky tests** (`neutral` / `Flaky-recovered > 0`): record the test, `@flaky` status, run-count, and the failure reason from the `## Failures` traceback.
4. **Slow jobs/tests**: flag anything egregiously slow for its value, or that bottlenecks the pipeline regardless of value (e.g. a single 5-min test). Investigate *every* slow job to find what caused the extra time -- break it down per-step (log timestamps) before blaming tests; e.g. `actions/checkout` with `fetch-depth: 0` does a full-history, all-branches fetch that can dominate wall-clock.
5. **Failures recurring across PRs**: one hard failure is a human's problem, but the same signature on multiple unrelated branches is a systemic issue a human looking at a single PR won't see.
6. **Coverage**: `test-offload` prints a coverage-delivery diagnostic; a `MISMATCH` line = dropped `.coverage` data.
7. **Repeated log noise**: a warning recurring at a fixed interval, even on a *passing* job, usually means a misconfig -- e.g. `test-docker-electron` (no Modal token) logging `Modal is not authorized` every ~10s. The text usually names the fix.

## Don't over-claim (common traps)

- Always read the actual failure output before attributing a cause. Hard failures are usually branch-specific gates (ratchet sync, coverage, stale generated CLI docs, snapshot mismatches), not infra; Modal acceptance flakiness is usually absorbed by retries (-> neutral), so it rarely causes the hard failure.
- Cross-branch duration differences are mostly test-phase variance; the cached base build (~45s) and env-prep (~60s) are near-constant. Only flag a regression if *setup/cache* steps grew or the *same* branch slowed across runs.
- Normal Modal host-creation output (not errors/cache-misses): `building from mngr default Dockerfile` (cheap COPY layer, ~2s) and `<pkg> ... Installing at runtime` (only `test_mngr_create_with_dockerfile_on_modal`, not every host).
- One broken branch is not a CI-health issue: a WIP branch failing repeatedly with the same signature on files it added is normal; only the same failure across unrelated branches is systemic.

## Report

Group by category, most important first. Per finding: what, where (run/check URL + test/job), frequency across sampled runs, and the diagnostic detail (a flake's failure reason; which step made a job slow). Separate infra noise from real regressions, and already-being-fixed (check `gh pr list`) from open. State how many runs you sampled and over what window. Don't dump raw logs.
