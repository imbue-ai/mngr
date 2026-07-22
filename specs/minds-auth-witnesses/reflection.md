# Reflection: witnessing the minds authentication corpus with tmr-specs

Layer-3 of the spec-anchored testing stack. This document reflects on Phase 1
(the hand audit), Phase 2 (a hand-witnessing pass, two `tmr-specs-minds` fleet
runs on Modal that both failed to produce witnesses, a blocked `local` pilot,
and a final local hand-generation batch), across the three axes the task asks
for: the quality of the generated witnesses, whether witnessing surfaced
implementation problems, and what the runs teach about the tmr-specs machinery
itself.

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
| After the two fleet runs | 8 | 17 | 7 | (unchanged: both runs produced nothing adoptable) |
| After local hand-generation | 14 | 12 | 6 |

The **6 remaining `none` are exactly the six `mngr_forward` bridge units** we
dropped from scope (`open-from-landing`, `direct-navigation`,
`signed-out-workspace`, `non-html-refused`, and the `single-credential` /
`credential-not-forwarded` rules). Every unit implemented on the `apps/minds`
side is now witnessed (full or an honest partial).

The 12 partials are honest, not gaps to churn on: the universally-quantified
invariant Rules (`single-use-codes`, `no-data-without-session`,
`sessions-unforgeable`, `signing-key-minted-once`, `no-open-redirects`,
`fetch-never-spends`); the two unit-level token-integrity witnesses
(`tampered-token`, `foreign-token`, whose HTTP-surface half is unwitnessed);
`consent-first` (only the no-return-destination case); and the three units that
are behaviourally complete but read as `partial` only because of the matrix's
per-marker aggregation (`missing-code`, `default-destination`, `consent-gate` —
see section 3).

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

This is the dominant machinery finding and is expanded in section 3.

## Local provider outcome: blocked by the Claude Code trust dialog

The direction after the Modal failures was to run locally. A single-agent
`--provider local` pilot (scoped to `expired-token`) failed at launch: the local
provider copies the checkout to `~/.mngr/copies/agent-<id>/` and Claude Code
refuses to start there — "Source directory ... is not trusted by Claude Code" — a
trust dialog that `--headless` cannot answer. Trust is stored per-directory in
`~/.claude.json`, so the fleet's dynamically-named copy dirs cannot be
pre-trusted without either interactive acceptance or a global trust-bypass change
to the user's Claude config (out of scope, security-sensitive). Net: the fleet is
blocked on *both* providers by distinct environment issues — Modal by the hanging
coverage command, local by the trust dialog — neither a fault in the specs,
prompts, or recipe logic. So the remaining witnesses were **hand-generated
locally**, which is what "generate the tests locally" ultimately meant here.

## 1. Quality of the generated witnesses

No *fleet* witnesses exist to assess — both runs produced none. What follows
assesses the witnesses I hand-generated locally (the Phase 2 hand pass plus the
post-Modal batch), held to the bar the mapper prompt sets for the fleet:

- **`fresh-code` (full):** drives the real `/login` -> JS redirect -> `/authenticate`
  flow; asserts landing on `/`, a signed-in follow-up request, and the code now
  spent. Every assertion traces to a step; no gold-plating.
- **`prefetch` + `fetch-never-spends`:** a scriptless fetch of the login URL sets
  no session and leaves the code spendable. `fetch-never-spends` keeps an honest
  `partial=` — a universally-quantified Rule ("any URL the system hands out")
  that one preloader scenario cannot fully witness.
- **`survives-restart` (full):** a cookie minted on one app instance still
  authenticates against a fresh instance on the same data directory — the actual
  observable, not just the signing-key-persistence mechanism.
- **`expired-token` (full):** the time-dependent unit. Rather than dodge it as
  `PARTIAL_STEADY` (it is *not* untestable in kind — only awkward without a
  clock), I mint a backdated cookie via a `TimestampSigner` subclass (no
  monkeypatch, no `freezegun`) and assert the HTTP surface treats the bearer as
  signed out. An `age_seconds=0` sanity assertion guards the construction against
  salt/payload drift, so the rejection is provably due to age. Helper in
  `testing.py`.
- **`used-code` / `unknown-code` (full):** tightened to also assert the refusal
  sets no session cookie (the "no session is established" step), not just the 403.
- **`signed-out-home` (full):** asserts the login-URL/terminal guidance *and* that
  a known workspace id is absent ("reveals nothing about existing workspaces").
- **`already-signed-in` (full):** proves the fresh code is still redeemable after
  opening `/login` while signed in (the "code remains unspent" step).
- **`deep-link-prefill` (full):** asserts branch prefill and advanced-fields-open,
  not just the git URL.

No gold-plating was introduced: every added assertion traces to a unit step or an
in-scope invariant. The partials left standing are honest — I did not force
behaviourally-complete outline/compound units to `full` by writing a redundant
single test just to satisfy the matrix's per-marker rule.

## 2. Implementation problems surfaced by witnessing

The fleet surfaced none (it produced no commits or escalations). Hand-generation
surfaced no implementation bugs either — every hand-written witness passed
against the current implementation on its first green run, and no `partial=`
residue traced to a divergence rather than an untestable quantifier.

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
- **The `local` provider is unusable headless due to Claude Code's trust
  dialog.** Each local agent runs in a fresh `~/.mngr/copies/agent-<id>/` copy
  that Claude Code treats as untrusted; `--headless` cannot accept the trust
  prompt, so every launch fails ("Source directory ... is not trusted"). Because
  trust is keyed per-directory in `~/.claude.json` and the copy dirs are minted
  per run, there is no non-interactive, non-global way to pre-trust them. For a
  headless fleet the provider (or `mngr`) needs to seed trust for the copy dir it
  just created before starting Claude there.
- **A clean exit hides a total failure.** Run 1 exited 0 with an all-`FAILED`
  report; a caller scripting on exit code alone would not notice zero units were
  witnessed. A non-zero exit (or a summary line) when no mapper succeeds would
  help.
- **The report rendered the failure cleanly** (six FAILED rows, empty matrix, a
  working reintegrate hint) — correct behavior for this state. Prompt-quality and
  report-quality of *successful* mapping could not be assessed, since no agent
  produced an outcome.
- **Secrets passed via `--env` are echoed in plaintext.** Launching with
  `--env "ANTHROPIC_API_KEY=<value>"` caused `mngr tmr-specs` to echo the
  fully-resolved command — key and all — into the run log and the launching
  agent's transcript. The launcher should redact known-secret `--env` values
  (or accept a secret by name/file reference) rather than printing the resolved
  command. This surfaced as a real leak during this task (the affected key was
  scrubbed from `/tmp` and should be rotated).

## Machinery feedback is feedback, not edits

Per the standing policy, none of the observations above were acted on by
editing `libs/mngr_tmr` or the read-only corpus. They are recorded here for
Danver's consideration only.
