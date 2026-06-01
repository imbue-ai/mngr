---
name: audit-ci
description: Audit recent CI runs for anomalies (warnings, uncached docker builds, flaky/slow tests, regressions).
---

Identify anomalies in recent CI runs and produce a report. If you fan out with subagents, ensure they follow the guidelines below (they don't inherit this file), and verify load-bearing claims at the source yourself.

## Where results live (counterintuitive)

In `.github/workflows/ci.yml`, the release tests run only on `release`-branch pushes, so a normal PR/main audit never sees them.

**Test results are not in the workflow jobs.** Each test job ends by POSTing a *separate* check-run (the `report-flaky-aware-tests` action) named `Unit + Integration Tests` / `Acceptance Tests`. They show as `in 0s` with `/runs/<id>` URLs (the PR "Checks" format, not `/actions/runs/.../job/`). Fetch the retry/flaky table:
`gh api repos/imbue-ai/mngr/check-runs/<id> --jq '{name,conclusion,summary:.output.summary}'`
- Conclusion: `success` = clean; **`neutral` = passed only after retries (the flaky signal)**; `failure` = hard fail.
- Summary has a `| Test | Runs | Final | @flaky |` table; `Final` reads `flaked N, passed M` or `passed`. **Highest-signal: `flaked N` with `@flaky = no`.** A high run-count with `Final = passed` is just offload's by-design reruns, not a flake.

## Data sources (cheap -> expensive)

- `gh run list --workflow=CI --limit N --json databaseId,headBranch,event,conclusion,displayTitle` -- enumerate runs.
- `gh run view <run_id>` -- jobs, durations, and an **ANNOTATIONS** section (warnings + failure messages, no log download). Start here.
- `gh pr checks <pr>` -- PR -> its check-runs and job URLs.
- `gh run view --job <id> --log` -- full log; write to a file before grepping (per CLAUDE.md). Only when annotations don't explain it.

## What to look for

1. **Warnings** (the ANNOTATIONS section, including on otherwise-passing runs).
2. **Uncached image rebuilds**: offload caches its base image in git notes (`refs/notes/offload-images`, ~48h TTL), so occasional misses are fine -- flag *frequent*/no-change misses, or a missing `contents: write` perm / failed `git fetch refs/notes/*` (defeats the cache every run). Grep logs for base-image build / `cache miss`.
3. **Flaky tests** (`neutral` / `Flaky-recovered > 0`): record the test, `@flaky` status, run-count, and -- by reading the `## Failures` traceback -- **whether it's a timeout or an error.** That classification is the key fact a fixer needs:
   - *Timeout* (`Timeout (>Ns) from pytest-timeout`): body/teardown exceeded the budget (`--timeout=10`). Note *where* the time went; if the slow part isn't essential to what the test verifies, making it faster beats bumping `@pytest.mark.timeout(N)`. `@flaky` is wasteful here -- it spends CI reruns on a test that isn't actually broken.
   - *Error* (Modal `app is locked ... Please retry`, network blip): a rerun is the matching remedy, so `@flaky` / offload-retry fits.
4. **Slow jobs/tests**: compare durations across runs. For a slow *job*, break it down per-step (log timestamps) before blaming tests -- e.g. `actions/checkout` with `fetch-depth: 0` does a full-history, all-branches fetch that can dominate wall-clock (and varies with GitHub's pack-serving).
5. **Hard failures**: same signature on multiple unrelated PRs (infra) or just one branch (its own bug)?
6. **Coverage**: `test-offload` prints a coverage-delivery diagnostic; a `MISMATCH` line = dropped `.coverage` data.
7. **Repeated log noise**: a warning recurring at a fixed interval, even on a *passing* job, usually means a misconfig -- e.g. `test-docker-electron` (no Modal token) logging `Modal is not authorized` every ~10s. The text usually names the fix.

## Don't over-claim (common traps)

- Always read the actual failure output before attributing a cause. Hard failures are usually branch-specific gates (ratchet sync, coverage, stale generated CLI docs, snapshot mismatches), not infra; Modal acceptance flakiness is usually absorbed by retries (-> neutral), so it rarely causes the hard failure.
- Cross-branch duration differences are mostly test-phase variance; the cached base build (~45s) and env-prep (~60s) are near-constant. Only flag a regression if *setup/cache* steps grew or the *same* branch slowed across runs.
- Normal Modal host-creation output (not errors/cache-misses): `building from mngr default Dockerfile` (cheap COPY layer, ~2s) and `<pkg> ... Installing at runtime` (only `test_mngr_create_with_dockerfile_on_modal`, not every host).
- One broken branch is not a CI-health issue: a WIP branch failing repeatedly with the same signature on files it added is normal; only the same failure across unrelated branches is systemic.

## Report

Group by category, most important first. Per finding: what, where (run/check URL + test/job), frequency across sampled runs, and the diagnostic a fixer needs (flake -> timeout-vs-error + where the time went; slow job -> which step). Separate infra noise from real regressions, and already-being-fixed (check `gh pr list`) from open. State how many runs you sampled and over what window. Don't dump raw logs.
