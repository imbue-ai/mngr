# Offload v0.9.0 Upgrade with History-Based Scheduling

## Goal

Upgrade offload from 0.8.1 to 0.9.0 and enable the `[history]` feature to
improve test scheduling via per-test duration estimates. Demonstrate concrete
wall-clock improvement by comparing before/after CI runs on warm caches.

## Current State (main, offload 0.8.1)

- 10,662 tests across ~190 sandboxes (max_parallel=50 for unit/integration)
- Sandbox times range 2.4s-43.9s (18x spread) due to naive round-robin scheduling
- No history data; offload has no duration estimates for test batching
- Wall-clock time gated by slowest sandbox (~44s sandbox time + overhead)

## Measurement Methodology

- **Before**: CI run on this branch with zero code changes (only plan + changelog).
  Uses offload 0.8.1 from CI pin. Warm cache (no Dockerfile/pyproject/uv.lock changes).
- **After**: CI run on this branch after adding `[history]` config and seeded history
  files. Uses offload 0.9.0 from updated CI pin. Warm cache (same checkpoint image).
- Both runs use the `test-offload` job. Compare wall-clock time, max/min/mean sandbox
  times from junit.xml.

## Plan

### Step 1: Record "before" baseline via CI (offload 0.8.1)

Push this branch with only plan + changelog. CI triggers with current 0.8.1 config.
This gives us the warm-cache baseline to compare against.

Deliverable: CI run URL and timing data from junit.xml artifact.

### Step 2: Add `[history]` sections to offload TOML configs

Add to each config file:

- `offload-modal.toml` -> `[history] path = "offload-history-modal.jsonl"`
- `offload-modal-acceptance.toml` -> `[history] path = "offload-history-modal-acceptance.jsonl"`
- `offload-modal-release.toml` -> `[history] path = "offload-history-modal-release.jsonl"`

One history file per config since the test sets are disjoint.

### Step 3: Seed history with 3 local runs

Run `just test-offload --record-history` three times locally to build up duration
estimates in `offload-history-modal.jsonl`. Each run refines the per-test timing data
that offload uses for LPT (Longest Processing Time) scheduling.

Three runs give enough signal for stable duration estimates, smoothing out cold-start
noise from individual runs.

Commit the history file after each run so the next run benefits from the prior data.

### Step 4: Set up git merge driver for history files

Run `offload history setup-merge-driver` for each config to configure `.gitattributes`
and local git config. This ensures JSONL history files merge cleanly across branches
rather than producing conflicts.

### Step 5: Update CI to offload 0.9.0

In `.github/workflows/ci.yml`:

- Bump `cargo install offload@0.8.1` to `offload@0.9.0` (both test-offload and
  test-offload-acceptance jobs)
- Add `--record-history` to offload invocations so history accumulates across CI runs
- Cache history files alongside or instead of junit.xml duration cache
- Update cargo cache key from `cargo-offload-0.8.1` to `cargo-offload-0.9.0`

### Step 6: Update justfile recipes

Add `--record-history` flag to `just test-offload` and `just test-offload-acceptance`
recipes so local runs also contribute to history.

### Step 7: Record "after" measurement via CI (offload 0.9.0, with history)

Push all changes. CI triggers with 0.9.0 + seeded history files. Warm cache
(checkpoint image unchanged since Step 1 -- no Dockerfile/pyproject/uv.lock edits).

Deliverable: CI run URL and timing data from junit.xml artifact.

### Step 8: Report results

Compare before vs after:

- Total wall-clock time of test-offload job
- Max sandbox time (determines wall-clock)
- Min sandbox time
- Mean / std-dev of sandbox times
- Sandbox time distribution histogram

## Constraints

- Before and after runs must both be warm-cache (no checkpoint rebuild) for fair
  comparison. This means no changes to Dockerfile, pyproject.toml, or uv.lock between
  the two measurements.
- History files are committed to the repo so all branches and CI benefit from them.
- The merge driver must be set up so concurrent branches don't conflict on history files.
