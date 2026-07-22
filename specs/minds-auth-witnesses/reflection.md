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
- **Provider: Modal** (`--provider modal`), spend approved. The fleet ran 6
  mappers (one per feature file) + 1 reducer over the whole authentication
  corpus with the default `apps/minds` test root.

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

## Fleet run outcome: total timeout, zero adoptable output

The first Modal fleet run (`20260721082621`) produced **nothing adoptable**: all
six mappers were stopped at the 3600s per-agent timeout "without publishing
outputs", so no branch was pulled, the reducer never ran, and the report's
coverage matrix is empty. The run still exited 0 (a clean "everyone failed"),
and it cost real money.

The likely cause is resource contention, not a slow task per se: `mngr ls` shows
the six mappers were packed **three-per-host onto just two Modal hosts**
(`host-0`, `host-1`). Three heavyweight Claude Code agents sharing one host —
each doing a cold `uv sync --all-packages` over the monorepo and then collecting
/ running a large pytest suite — almost certainly thrashed CPU/RAM/disk (the
hosts went unreachable near the end, "Could not connect ... Unable to connect
to port ...", consistent with an OOM or a wedged host). Under that contention
every agent crawled past 3600s before reaching even its first outcome write.

This is the dominant machinery finding of the exercise and is expanded in
section 3. Because the run yielded no witnesses, sections 1 and 2 below have no
fleet material to assess for this run.

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

Run-time friction observed this run:

- **Host packing starves heavyweight agents (the run-killer).** The fleet placed
  three mappers per Modal host. For a lightweight suite that is fine; for the
  minds monorepo — where each agent independently pays a cold `uv sync
  --all-packages` and a large pytest collection — three concurrent agents per
  host is enough to thrash it into the timeout (and, it appears, to knock the
  host offline). Two mitigations for a re-run: cap concurrency so agents do not
  share a host (`--max-parallel-agents` low enough, or one agent per host), and
  raise `--timeout` well above 3600s for this repo. A cheaper alternative is the
  `local` provider, which reuses the already-synced `.venv` and skips the
  per-host cold sync entirely.
- **The default 3600s timeout is mis-scaled for this repo.** Even without
  contention, a cold environment plus a first pytest run against the minds tree
  can approach or exceed an hour before the agent writes its first outcome. The
  timeout should be tuned per target repo (or the recipe should carry a
  minds-appropriate default), and the mapper prompt could ask agents to write a
  partial outcome early so a timeout still yields *something*.
- **A clean exit hides a total failure.** The run exits 0 with an all-`FAILED`
  report; a caller scripting on exit code alone would not notice that zero units
  were witnessed. A non-zero exit (or a summary line) when no mapper succeeds
  would help.
- Prompt-quality and report-quality (claimed-vs-verified matrix, escalations,
  normalizations) could not be assessed: no agent produced an outcome. The
  report itself rendered the failure cleanly (six FAILED rows, empty matrix,
  a working reintegrate hint), which is the correct behavior for this state.

## Machinery feedback is feedback, not edits

Per the standing policy, none of the observations above were acted on by
editing `libs/mngr_tmr` or the read-only corpus. They are recorded here for
Danver's consideration only.
