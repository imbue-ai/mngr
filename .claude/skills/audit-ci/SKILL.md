---
name: audit-ci
description: Audit recent CI runs for anomalies (warnings, uncached docker builds, flaky/slow tests, regressions). Use when asked to review CI health, find what's amiss in CI, or check recent pipeline runs.
---

Audit recent CI runs on this repo for anything amiss and produce a concise report. Investigate breadth-first across many runs, then drill into specifics. Use subagents to fan out over runs in parallel.

## How CI is laid out here (read this first -- it is counterintuitive)

The `CI` workflow (`.github/workflows/ci.yml`) has jobs: `test-offload` (unit+integration), `test-offload-acceptance`, `test-docker`, `test-docker-electron`, `cleanup-modal-environments`, plus release-only jobs (`test-release`, `test-docker-release`) that are skipped on PRs.

**The actual test results do not live in the workflow jobs.** At the end of each test job, the `report-flaky-aware-tests` composite action POSTs a *separate synthesized check-run* via `gh api .../check-runs`, named `Unit + Integration Tests` and `Acceptance Tests`. These are what you want:

- They show as `in 0s` in `gh run view` and have URLs like `github.com/imbue-ai/mngr/runs/<check_run_id>` (note `/runs/`, not `/actions/runs/.../job/`). This is the format in PR "Checks" tabs.
- Fetch their body (the retry + flaky summary table) with:
  `gh api repos/imbue-ai/mngr/check-runs/<check_run_id> --jq '{name,conclusion,title:.output.title,summary:.output.summary}'`
- Conclusion semantics: `success` = clean; **`neutral` (yellow) = tests passed only after flaky retries**; `failure` = hard fail. A neutral check is the primary flaky signal.
- The summary reports `Unique tests`, `Total runs`, `Tests that ran more than once`, `Tests marked @flaky`, `Flaky-recovered`, `Failing (final)`, plus a per-test table `| Test | Runs | Final | @flaky |`. The `Final` column reads e.g. `flaked 1, passed 8` for a recovered test or `passed` for a clean one. **The highest-signal finding is a row with `flaked N` in `Final` AND `@flaky = no`** -- an unmarked test that actually flaked, which should either be fixed or marked `@flaky`. Rows with high run-counts but `Final = passed` are just offload's by-design re-runs, not flakes.

Note: offload runs many tests multiple times by design (retry_count), so "ran more than once" being large (thousands) is normal and is NOT the flaky signal -- `Flaky-recovered > 0` or a `neutral` conclusion is.

## Data sources

- `gh run list --workflow=CI --limit N --json databaseId,headBranch,event,conclusion,createdAt,displayTitle` -- enumerate runs.
- `gh run view <run_id>` -- jobs, durations, and an **ANNOTATIONS** section that surfaces warnings (e.g. Node.js deprecations) and failure messages without downloading logs. Cheap; start here.
- `gh pr checks <pr>` -- maps a PR to its synthesized check-runs and job URLs.
- `gh run view --job <job_id> --log` / `gh run view <run_id> --log` -- full logs. Large; always write to a file first, then grep (per CLAUDE.md). Use only when annotations/check-runs don't explain something.
- Test timing: the per-test run-count table in the check-run summary; release runs upload `test_durations_*.json` artifacts.

## Anomaly categories to check

1. **Warnings / annotations** -- run `gh run view <id>` and read ANNOTATIONS. Node.js-version deprecations on actions, pytest deprecation/collection warnings, vulnerable-dependency advisories, GHA `set-output`/save-state deprecations.
2. **Uncached docker / image rebuilds** -- offload caches its base image via git notes (`refs/notes/offload-images`); TTL ~48h, so occasional misses are expected and fine. Flag *frequent* misses or misses with no triggering change. Grep job logs for base-image build/restore lines and "cache miss"; missing `contents: write` perms or failed `git fetch ... refs/notes/*` defeat the cache for every run. Also watch for the mngr-default-Dockerfile warning ("building from mngr default Dockerfile") inside test failures.
3. **Flaky tests** -- `neutral` check conclusion or `Flaky-recovered > 0`. Note which tests, whether they are `@flaky`-marked, and high run-counts.
4. **Slow tests / slow jobs** -- compare job durations across runs for the same branch/event; flag outliers and the slowest individual tests.
5. **Hard failures** -- distinguish real failures from infra flakiness (Modal deploy errors, network). Check whether the same failure hits multiple unrelated PRs (infra) or just one (likely code).
6. **Coverage** -- `test-offload` prints a coverage-delivery diagnostic; a `MISMATCH` line (sandboxes delivering junit but not `.coverage`) signals dropped coverage data.
7. **Cross-run patterns** -- the same test/job misbehaving across many runs matters more than a one-off.

## Calibration -- do not over-claim (these are easy traps)

- **Don't conflate failure messages with warnings.** A failing run's ANNOTATIONS include its test-failure tracebacks; those belong to failure triage, not the "warnings" bucket. A genuine warning is one that appears on *passing* runs too.
- **Job-duration differences between branches are mostly test-execution variance, not regressions.** The cached base-image build is near-constant (~45s) and env-prep ~60s; what varies is the test phase (different test selection + flaky-retry count). Only call a duration regression if the *setup/cache* steps grew or the same branch slowed across successive runs -- not because branch X's `test-offload` took 9m vs branch Y's 7m.
- **These Modal lines are normal host-creation output, not errors or cache misses:** `WARNING: No image or Dockerfile specified -- building from mngr default Dockerfile` (it's a cheap COPY layer on the cached base, ~2s) and `WARNING: <pkg> is not pre-installed in the base image. Installing at runtime`. They appear on every acceptance run. (The runtime-install lines are a mild, repeated startup cost worth mentioning once, but they are not failures.)
- **`test-docker-electron` runs with no Modal token by design,** so its log floods with `WARNING: Discovery error from modal: Modal is not authorized`. Expected environment noise, not a real failure.
- **One broken branch is not a CI-health problem.** A WIP/PR branch failing many consecutive times with the *same* signature (e.g. a ratchet/git-blame error on files it added) is normal. Only the *same failure across multiple unrelated branches* indicates a systemic issue. Always confirm a "systemic Modal" claim by reading the actual failing job -- hard failures are usually branch-specific gates (ratchet sync, coverage, out-of-date generated CLI docs, snapshot/inline-snapshot mismatches) rather than infra.
- **Verify the actual failing job before attributing a cause.** Modal flakiness in acceptance tests is real but is usually *absorbed by retries* (→ neutral check), so it rarely causes the hard failure; the hard failure is often something else in a different job.

## Producing the report

Group findings by category, most to least important. For each: what, where (run/check URL + test or job name), how often across the sampled runs, and a suggested action. Separate infra noise from real regressions, and separate already-being-fixed items (check for an open PR -- e.g. grep `gh pr list` for the warning) from open ones. State how many runs you sampled and over what window. Be concise; do not dump raw logs.
