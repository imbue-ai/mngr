# Reflection: witnessing the minds authentication corpus with tmr-specs

Layer-3 of the spec-anchored testing stack. This document reflects on Phase 1
(hand audit), the Phase 2 hand-witnessing pass, and the Phase 2 fleet run
(`tmr-specs-minds` on Modal), across the three axes the task asks for: the
quality of the generated witnesses, whether witnessing surfaced implementation
problems, and what the run teaches about the tmr-specs machinery itself.

Companion document: `audit.md` (the Phase 1 map and before/after matrix).

## Scope decisions taken during the run

- **The corpus spans two systems.** `apps/minds/specs/authentication` describes
  one surface but is implemented across the minds desktop client (`apps/minds`)
  and the workspace-origin bridge (`libs/mngr_forward`). The four
  `workspace-bridge.feature` scenarios and the `single-credential` /
  `credential-not-forwarded` rules are `mngr_forward`'s; everything else is the
  desktop client's. This split is the single most important structural fact of
  the exercise.
- **Decision (Danver): drop `mngr_forward` entirely; witness only
  authentication on the `apps/minds` side.** An earlier plan to mark the
  existing `mngr_forward` tests and widen the matrix (`--tests apps/minds
  --tests libs/mngr_forward`) was dropped: `mngr_forward` has not itself been
  specced/witnessed as its own system, and mixing it in muddied the run. The
  clean long-term model is one corpus per system (a future
  `libs/mngr_forward/specs/`), so `tmr-specs` runs cleanly per system. As a
  result, the six `mngr_forward`-owned units are intentionally left unwitnessed
  from `apps/minds` and read as `none`/blocked here.
- **Provider: two Modal runs, then a pivot to `local`.** Both Modal runs (6
  mappers + reducer over the authentication corpus, default `apps/minds` test
  root) failed to produce witnesses (see "Fleet run outcome"). Because the
  root-cause command works locally, the direction became to generate the tests
  on the `local` provider / by hand.

## Coverage trajectory (`mngr specs matrix --root apps/minds/specs`)

| Stage | full | partial | none |
|-------|-----:|--------:|-----:|
| Baseline (pre-audit) | 0 | 0 | 32 |
| After Phase 1 hand audit | 5 | 17 | 10 |
| After Phase 2 hand-witnessing | 8 | 17 | 7 |
| After fleet adoption | 8 | 17 | 7 | (unchanged: the fleet produced nothing adoptable) |

The Phase 2 hand-witnessing added three tests: a full end-to-end `fresh-code`
sign-in, a `prefetch` / `fetch-never-spends` scriptless-fetch test, and a
`survives-restart` cross-instance session test. Of the 7 remaining `none`, six
are the `mngr_forward` bridge units (out of scope) and one is `expired-token`
(needs control over the 30-day `max_age` clock, deliberately left for the
fleet to see how it handles a time-dependent unit).

## Fleet run outcome: two Modal runs, zero adoptable witnesses

Two Modal fleet runs were attempted; neither produced an adoptable witness.

**Run 1** (`20260721082621`, default `--agents-per-host 4` which placed 3 mappers
on each of 2 hosts, `--timeout 3600`): all six mappers were stopped at the 3600s
timeout "without publishing outputs" — no branch pulled, no reducer, empty
coverage matrix, clean exit 0. My first hypothesis was host contention (three
heavyweight agents thrashing one host), so run 2 changed the packing.

**Run 2** (`20260721205010`, `--agents-per-host 1` so each mapper got its own
host, `--timeout 7200`): the launcher then crashed ~88 min in on a *transient*
Modal `ServiceError: Authorization check failed` during polling (a Modal-side
glitch, not our credentials — `mngr ls` worked again immediately after), so it
never polled the mappers to completion, pulled outputs, or ran the reducer (exit
1). Crucially the six mappers *survived* the launcher crash and were still
RUNNING on their dedicated hosts, which let me diagnose them directly.

**Root cause — the contention hypothesis was wrong.** With one agent per host and
no contention, the mappers *still* made no progress: ~90 minutes with zero file
writes after venv setup, no commits, no outputs, no pytest running, only 3–14
minutes of CPU each. A surviving agent's Claude transcript pinned the stall
exactly — the mapper's last tool call was

    uv run mngr specs matrix --root apps/minds/specs --tests apps/minds

(the coverage command the mapper prompt tells every agent to run early: "To find
the current witnesses of your units, run the coverage matrix"), and that Bash
call **never returned** — the transcript dead-ends on it with no tool_result for
the rest of the run. Every mapper blocked on the same command. The identical
command completes in *seconds* on the local dev machine, so the hang is specific
to running it on the Modal host. `mngr specs matrix --tests apps/minds` drives a
`pytest --collect-only` over the whole `apps/minds` tree (plus `uv run`'s env
resolution); the likely mechanism is a collection-time hang in that large suite
on the Modal host (a conftest/import that probes for docker/modal/network and
blocks where they are absent) and/or `uv run` re-resolving — either way the outer
command does not return, and the framework's inner 300s collection timeout does
not save it. So host packing (run 1) was at most secondary; the primary,
run-killing bug is the hanging coverage command, which no timeout or concurrency
change addresses. This is why the layer-3 direction became: **run locally**,
where that command works.

This is the dominant machinery finding and is expanded in section 3. Because
neither run yielded witnesses, sections 1 and 2 have no fleet material to assess.

## 1. Quality of the generated witnesses

No witnesses were generated: every mapper timed out before publishing. There is
nothing to assess for adoption. The apps/minds authentication coverage reported
above (8 full / 17 partial / 7 none) is entirely from the Phase 1 audit and the
Phase 2 hand-witnessing, both of which stand on their own.

_[If a corrected re-run is authorized, complete this section: assess each
generated witness test-by-test — honest witnessing vs gold-plating, accurate
`partial=` notes, `PARTIAL_STEADY` only for residue untestable *in kind* (watch
`expired-token`, which IS testable via a backdated token, and the invariant
Rules), and correct placement per the repo test taxonomy.]_

## 2. Implementation problems surfaced by witnessing

The fleet surfaced none (it produced no commits or escalations). The
hand-witnessing pass surfaced no implementation bugs either — every
hand-written witness passed against the current implementation.

One divergence noted during the audit (for cross-checking against any
fleet escalation): the `mngr_forward` sign-in bridge returns **403** for a
missing/empty one-time code, whereas the desktop client returns **422**
(`missing-code` specifies 422). Because the corpus does not scope which surface
`missing-code` binds, this is a latent ambiguity — but it is out of scope here
since we dropped `mngr_forward`.

## 3. What the run teaches about the tmr-specs machinery

Observations that do NOT depend on the fleet outcome (found by reading the
recipe, prompts, and report code, and confirmed against the matrix):

- **Multi-system corpora have no clean fan-out unit.** `tmr-specs` fans out one
  mapper per `.feature` file and scopes only by `--area` (folder), `--tag` (one
  unit), or `--unit` (kind). There is no per-system or per-file selector, so a
  corpus whose units are implemented across two projects cannot be pointed at
  one system's tests in a single run. This is the friction that forced the
  "drop mngr_forward" decision. The structural fix is one corpus per system,
  which the layer-1 corpus model already supports (per-project `specs/`).
- **The matrix understates coverage for outline / compound units.**
  `compute_spec_coverage` marks a unit `full` only when some *single* marker
  omits `partial=`. A scenario outline covered by one test per example row
  (`missing-code`: `/login` + `/authenticate`; `default-destination`:
  has/no-workspaces), or a compound scenario covered by one test per When/Then
  pair (`consent-gate`: shown-after-signin + never-again), is genuinely,
  behaviourally complete yet reads as `partial`. This is by design (one test is
  meant to stand as the full witness), but it means a future run may churn on
  units that are already fully covered, and the report's claimed-vs-verified
  matrix will show a standing disagreement that is not a real gap. Worth a note
  in the report, or a coverage rule that treats "every example row / clause
  witnessed" as full.
- **`--tests` overrides rather than unions.** Passing any `--tests` replaces the
  default (corpus root's parent) instead of adding to it, in both the matrix CLI
  and `effective_test_roots`. A multi-root corpus therefore has to name every
  root explicitly; forgetting the default parent silently drops its witnesses.
- **Parametrized witnesses expand per node.** The `no-open-redirects` guard
  shows 14 matrix witnesses from 4 markers because two `responses_test`
  parametrizations contribute 4 + 8 nodes. Harmless, but noisy in the report.

Run-time friction observed across the two runs:

- **The mapper prompt's coverage command hangs on this repo/host (the actual
  run-killer).** Every mapper runs `uv run mngr specs matrix --root
  apps/minds/specs --tests apps/minds` early because the prompt instructs it
  ("To find the current witnesses of your units, run the coverage matrix"). On
  the Modal host that command never returns, so every agent blocks on it
  indefinitely and produces nothing — in *both* runs, independent of host
  packing or timeout. It is fast locally. Fixes to consider: (a) make
  `harvest_witness_links`' `pytest --collect-only` robust to collection-time
  hangs on this repo — the existing 300s inner timeout did not save the outer
  `uv run mngr` invocation, so the guard is at the wrong layer; (b) narrow the
  collection to the paired test tree; or (c) have the mapper prompt read current
  coverage from a pre-computed artifact the orchestrator supplies, rather than
  each agent re-running a full-suite collection. This single issue is why the
  fleet must run where that command works (the `local` provider).
- **A launcher crash orphans running agents with no timeout enforcement.** Run
  2's launcher died on a *transient* Modal auth error mid-poll; the six agents
  kept running on their hosts with the launcher's `--timeout` no longer
  enforced, so they would have burned compute indefinitely had I not stopped
  them. The launcher should treat a transient provider error during polling as
  retryable (it recovered on the very next call), and/or agents should
  self-bound their own wall-clock. `--reintegrate` *does* re-pull each agent's
  outputs and re-run the reducer (a good recovery primitive) — but only once the
  mappers actually finish, which here they never did.
- **A clean exit hides a total failure.** Run 1 exited 0 with an all-`FAILED`
  report; a caller scripting on exit code alone would not notice zero units were
  witnessed. A non-zero exit (or a summary line) when no mapper succeeds would
  help.
- **The report rendered the failure cleanly** (six FAILED rows, empty matrix, a
  working reintegrate hint) — correct behavior for this state. Prompt-quality and
  report-quality of *successful* mapping could not be assessed, since no agent
  produced an outcome.

## Machinery feedback is feedback, not edits

Per the standing policy, none of the observations above were acted on by
editing `libs/mngr_tmr` or the read-only corpus. They are recorded here for
Danver's consideration only.
