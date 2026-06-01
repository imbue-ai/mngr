---
name: audit-ci
description: Audit recent CI runs for anomalies (warnings, uncached docker builds, flaky/slow tests, regressions). Use when asked to review CI health, find what's amiss in CI, or check recent pipeline runs.
---

Identify (don't fix) anomalies in recent CI runs and produce a report. Work breadth-first, then drill in. Fan out with subagents -- but they don't see this file and reliably over-claim (variance -> "regression", one branch -> "systemic"), so paste the relevant caveats below into their prompts, or have them return only raw evidence (URLs, log lines, durations) and make the severity calls yourself. Verify any load-bearing claim at the source.

## Where results live (counterintuitive)

`.github/workflows/ci.yml` jobs: `test-offload` (unit+integration), `test-offload-acceptance`, `test-docker`, `test-docker-electron`, `cleanup-modal-environments` (+ release-only `test-release`/`test-docker-release`, skipped on PRs).

**Test results are not in the jobs.** Each test job ends by POSTing a *separate* check-run (the `report-flaky-aware-tests` action) named `Unit + Integration Tests` / `Acceptance Tests`. They show as `in 0s` with `/runs/<id>` URLs (the PR "Checks" format, not `/actions/runs/.../job/`). Fetch the retry/flaky table:
`gh api repos/imbue-ai/mngr/check-runs/<id> --jq '{name,conclusion,summary:.output.summary}'`
- Conclusion: `success` = clean; **`neutral` = passed only after retries (the flaky signal)**; `failure` = hard fail.
- Summary has a `| Test | Runs | Final | @flaky |` table; `Final` reads `flaked N, passed M` or `passed`. **Highest-signal: `flaked N` with `@flaky = no`.** A high run-count with `Final = passed` is just offload's by-design reruns, not a flake.

## Data sources (cheap -> expensive)

- `gh run list --workflow=CI --limit N --json databaseId,headBranch,event,conclusion,displayTitle` -- enumerate runs.
- `gh run view <run_id>` -- jobs, durations, and an **ANNOTATIONS** section (warnings + failure messages, no log download). Start here.
- `gh pr checks <pr>` -- PR -> its check-runs and job URLs.
- `gh run view --job <id> --log` -- full log; write to a file before grepping (per CLAUDE.md). Only when annotations don't explain it.

## What to look for

1. **Warnings** (ANNOTATIONS): GHA action deprecations (e.g. Node 20), pytest deprecation/collection warnings, vuln-dep advisories.
2. **Uncached image rebuilds**: offload caches its base image in git notes (`refs/notes/offload-images`, ~48h TTL), so occasional misses are fine -- flag *frequent*/no-change misses, or a missing `contents: write` perm / failed `git fetch refs/notes/*` (defeats the cache every run). Grep logs for base-image build / `cache miss`.
3. **Flaky tests** (`neutral` / `Flaky-recovered > 0`): record the test, `@flaky` status, run-count, and -- by reading the `## Failures` traceback -- **whether it's a timeout or an error.** That classification is the key fact a fixer needs (the remedy can't be chosen without it):
   - *Timeout* (`Timeout (>Ns) from pytest-timeout`): body/teardown exceeded the budget (`--timeout=10`). Note *where* the time went; if the slow part isn't essential to what the test verifies, making it faster beats bumping `@pytest.mark.timeout(N)`. Reruns don't fix latency, so an `@flaky` mark on a timeout-flake is mis-applied.
   - *Error* (e.g. Modal `app is locked ... Please retry`, network blip): a rerun is the matching remedy, so `@flaky` / offload-retry fits.
4. **Slow jobs/tests**: compare durations across runs. For a slow *job*, break it down per-step (log timestamps) before blaming tests -- e.g. `actions/checkout` with `fetch-depth: 0` does a full-history, all-branches fetch that can dominate wall-clock (and varies with GitHub's pack-serving).
5. **Hard failures**: same signature on multiple unrelated PRs (infra) or just one branch (its own bug)?
6. **Coverage**: `test-offload` prints a coverage-delivery diagnostic; a `MISMATCH` line = dropped `.coverage` data.
7. **Repeated log noise**: a warning recurring at a fixed interval, even on a *passing* job, usually means a misconfig -- e.g. `test-docker-electron` (no Modal token) logging `Modal is not authorized` every ~10s. The text usually names the fix.

## Don't over-claim (common traps)

- A failing run's ANNOTATIONS include its own tracebacks -- that's failure triage, not "warnings." A real warning appears on *passing* runs too.
- Cross-branch duration differences are mostly test-phase variance; the cached base build (~45s) and env-prep (~60s) are near-constant. Only flag a regression if *setup/cache* steps grew or the *same* branch slowed across runs.
- Normal Modal host-creation output (not errors/cache-misses): `building from mngr default Dockerfile` (cheap COPY layer, ~2s) and `<pkg> ... Installing at runtime` (only `test_mngr_create_with_dockerfile_on_modal`, not every host).
- One broken branch is not a CI-health issue: a WIP branch failing repeatedly with the same signature on files it added is normal. Hard failures are usually branch-specific gates (ratchet sync, coverage, stale generated CLI docs, snapshot mismatches) -- read the failing job before blaming "infra"/Modal. Modal acceptance flakiness is usually absorbed by retries (-> neutral), so it rarely causes the hard failure.

## Report

Group by category, most important first. Per finding: what, where (run/check URL + test/job), frequency across sampled runs, and the diagnostic a fixer needs (flake -> timeout-vs-error + where the time went; slow job -> which step). Separate infra noise from real regressions, and already-being-fixed (check `gh pr list`) from open. State how many runs you sampled and over what window. Don't dump raw logs.
