# TMR: bounded test convergence and suite-level normalization

Status: **partially implemented.** The prompt-level core and the report plumbing are
done (see "Implementation status" below); the heavier orchestration is still a follow-up.
Captures the design converged on while discussing how to improve the e2e tests TMR
generates.

**Audience:** developers working on `libs/mngr_tmr` (the test map-reduce recipe) and the
e2e suite under `libs/mngr/imbue/mngr/e2e/`.

**Related:** `libs/mngr_tmr/README.md`, `libs/mngr_mapreduce/README.md`,
`libs/mngr_tmr/imbue/mngr_tmr/prompt_assets/mapper.j2`,
`libs/mngr_tmr/imbue/mngr_tmr/prompt_assets/reducer.j2`.

## Overview

TMR runs an e2e test suite by fanning out **one testing agent per test** (the mapper),
each on its own branch and blind to the others, then a **reducer** that mechanically
integrates the branches (filter by `should_pull`, squash `[FIX_TEST]`/`[IMPROVE_TEST]`,
cherry-pick `[FIX_IMPL]` by priority). Two recurring problems both trace to the same
structural cause -- the mapper optimizes a single test *in isolation*, so there is no
representation of the suite as the optimization target:

1. **Unconstrained growth.** The mapper prompt is effectively monotonic ("verify as
   thoroughly as possible," "add edge cases," "add happy/unhappy paths"). An objective
   with no maximum and no subtractive move can only grow. Trivial tests (help/flag
   sanity checks) accrete assertions indefinitely.

2. **Unwillingness to make cross-cutting setup changes.** Duplication (e.g. each agent
   re-mocking an agent with `sleep`, or independently writing the same `mngr list`
   verification step) and shared-setup gaps (e.g. no canonical fake-claude fixture) are
   only visible *across* tests. A single isolated mapper structurally cannot see them.

The fix is to (a) give the mapper a bounded, two-sided objective anchored to a fixed
external reference, and (b) add a single suite-level normalization step that does the
cross-test work the mappers cannot. We deliberately **preserve the map -> reduce shape**;
the reduce step grows a second internal stage rather than introducing a new top-level
phase.

## Goals

- The complexity of a generated test **converges to a stable steady state** instead of
  growing every run.
- Cross-cutting duplication is **extracted into shared utilities** -- except where the
  duplication mirrors the tutorial (which must stay 1:1).
- Setup blockers that no single mapper can resolve are **resolved globally when safe**, and
  **escalated to the user** when not.
- Keep the top-level architecture **map -> reduce**; keep the integrate step cheap and
  always-succeeding.

## Non-goals

- A per-run heavyweight "planner" agent that reads the whole suite. The suite-level
  knowledge that must persist (relative importance, settled altitude) lives in durable,
  human-reviewable metadata alongside each test, not recomputed per run.
- Hand-authoring per-test verification checklists. The properties to check are derived by
  AI from the tutorial; we constrain the *objective*, not the content.

## Design

### 1. Bounded convergence in the mapper

A process converges only if it has a fixed point that **exists** and is **attracting**.
The current objective has neither: "as thorough as possible" has no maximum, and the only
moves are add/hold, so an overshot test can never come back down.

**Anchor the objective to the tutorial block.** Each test exists to verify the claims its
paired tutorial block makes (recorded verbatim via `e2e.write_tutorial_block`; ground
truth is `libs/mngr/imbue/mngr/resources/mega_tutorial.sh`). The block is fixed, so it
defines a fixed point that exists and does not move:

> Every claim the block makes is covered, and every assertion traces back to a claim the
> block makes.

This stays AI-derived from the tutorial -- it only adds the **backward** direction the
prompt currently lacks. Today the mapper runs tutorial -> assertions monotonically. The
fixed point requires also running it backward: an assertion that traces to no claim the
block makes is gold-plating and is a **candidate for removal**.

**Make deletion a first-class action.** This is the single most important change: without
a subtractive move, convergence from overshoot is impossible. A simplification (cutting an
over-fitted assertion) must be a valid, *credited* `IMPROVE_TEST` outcome, exactly like an
addition. "No change needed" must be a first-class success, not a grudging fallback the
prompt's gravity pushes against.

A consequence worth stating: test complexity becomes **pinned to tutorial complexity** -- a
human-meaningful, human-controlled quantity -- instead of free-running.

**Alongside-the-test metadata (descriptive, not prescriptive).** A stateless, re-spawned
mapper has two limits no prompt fixes:

- It **jitters** around the fixed point (two agents disagree at the margin on whether an
  assertion "traces to a claim"), producing churn.
- It **cannot judge relative importance** ("this core flow matters more than that `--help`
  aside"), which is an inherently cross-test comparison.

Both are closed by a small marker/note carried *next to the test function* that records the
altitude the suite already settled on (e.g. "tier-3 sanity test, deliberately minimal --
converged") and its relative importance. This is not "what to check"; it is the memory that
statelessness otherwise denies, and the one judgment (relative importance) no isolated agent
can make. Exact form (pytest marker vs. structured docstring) is an open question; it must
live alongside the test, per the constraint that properties are AI-generated and should not
be pinned in a separate file.

### 2. Suite-level normalization (reduce becomes integrate -> normalize)

The reduce step today is a mechanical git integrator. The cross-cutting work is *semantic*
and needs test-running, with the opposite risk profile. Split reduce into two stages:

- **Integrate** (unchanged): mechanical cherry-pick/squash. Cheap, robust, **always
  produces a merged branch**.
- **Normalize** (new): operates on the *already-integrated tree*, runs tests to verify, and
  may legitimately fail or time out. Publishing happens on top of a merge that is already
  safe -- if normalize fails, you still ship the integrated branch plus a report noting
  "normalization incomplete."

Whether normalize is one agent with two phases or a separately re-runnable top-level step is
an implementation detail; start with a sub-stage.

**Extraction predicate (the tutorial-1:1 nuance).** Duplication that mirrors the tutorial
must stay duplicated literally (preserve the 1:1 test <-> tutorial relationship); only
incidental scaffolding should be extracted. This is nearly deterministic, not a judgment
call, because the tutorial source is ground truth and each test declares its own block:

> A duplicated step is extractable **iff it does not appear in this test's tutorial block.**

`mngr create` that came from the block stays inline; a `mngr list` verification step that is
*not* in this test's block is extractable. It is per-test (a command can be tutorial in one
test and scaffolding in another). Two guardrails: (a) the predicate handles correctness;
extraction still needs a **two-sided value check** (does the helper improve clarity?) --
"extract all duplication" over-DRYs the suite into unreadable indirection, the same
monotonic disease as assertion growth. (b) Extraction is **reducer-only**: duplication is
visible only with the global view, so the reducer detects and performs it; mappers cannot.

**FIXME lifecycle.** Local blockers a single mapper *can* notice (e.g. "I'm blocked
launching a real claude agent") are raised by the mapper as FIXME comments. The reducer
triages each, gated on verifiability and blast radius:

1. **Apply and fully verify** (build the fixture, run its blast radius green on offload) ->
   do it.
2. **Apply but cannot fully verify** (blast radius too large or unrunnable) -> skip and
   leave the FIXME for next run's mappers, or apply-and-flag. Bias to skip when the blast
   radius is large -- an unverified cross-cutting change that is subtly wrong poisons the
   *next* run, making every mapper debug it.
3. **Cannot apply** (environment/infra/credentials -- e.g. codex needs OpenAI credentials
   absent from the env) -> **escalate** as a first-class TMR output: in the HTML report and
   as a PR comment. This channel stays low-volume because tier 1 absorbs everything
   verifiable.

**Verification scoped to blast radius.** The reducer made the change, so it knows which
tests touch the extracted helper or new fixture -- that is the `-k`/node-id subset it runs,
not the whole suite. Cost is proportional to ambition. Apply-vs-escalate should also key on
**whether the blast radius includes agent-creating tests** (see below): all-non-agent ->
cheap, reliable, auto-verify; includes agent-creating tests -> costlier, real creds,
partially-unverified path -> bias toward flag/escalate.

### 3. Verification feasibility (empirical)

Running the e2e suite to verify normalization is feasible today:

- e2e release tests **run cleanly on offload** via `offload-modal-release.toml` /
  `just test-offload-release`. A single help test ran green in ~32s for **~$0.03**, with the
  base release image already checkpointed (warm path: thin-diff build in seconds, no cold
  20-minute build). The e2e fixture's **Modal-in-Modal** environment setup worked.
- **Unverified / higher-risk:** tests that create real agents (the `modal`-marked majority)
  need a real `ANTHROPIC_API_KEY` and exercise a heavier Modal-in-Modal agent-lifecycle path
  not covered by the probe. This is the slow, credential-dependent subset where FIXMEs and
  escalation live -- hence the apply-vs-escalate line is drawn at "blast radius includes
  agent-creating tests."
- **Credential-gate friction:** `test-offload-release` hard-requires `ANTHROPIC_API_KEY` up
  front even for tests that never create an agent. For the reducer to run arbitrary
  credential-free subsets, that blanket precondition should be relaxed or made
  subset-aware.

## Implementation status

Done (prompt-level + report plumbing, in `libs/mngr_tmr`):

- Mapper prompt (`prompt_assets/mapper.j2`): tutorial-anchored objective with forward +
  backward traceability, deletion as a first-class `IMPROVE_TEST`, "no change" as a valid
  outcome, and a `# FIXME(tmr): ...` channel for cross-cutting setup blockers.
- Reducer prompt (`prompt_assets/reducer.j2`): a normalize stage after cherry-pick that
  extracts non-tutorial scaffolding (the "not in this test's tutorial block" predicate) and
  triages `FIXME(tmr)` blockers into `normalizations` / `escalations`.
- Report (`report.py`, `report_assets/report.html.j2`): parses and renders
  `normalizations` and `escalations` so unresolved blockers are visible, not dropped.

Deferred (orchestration / larger surface):

- Wiring the reducer to actually run a blast-radius subset on offload (creds passthrough +
  the credential-gate relaxation), so it can verify-and-apply rather than rely on whatever
  it can run locally.
- Posting escalations as a PR comment (today they land only in the HTML report).
- The alongside-the-test metadata mechanism (recorded altitude + relative importance):
  reader, prompt injection, and populating the e2e tests. The tutorial-anchoring above
  bounds growth without it; the metadata is the jitter/importance refinement.

## Open questions

- Exact form of the alongside-the-test metadata (pytest marker vs. structured docstring).
- Whether to relax the `test-offload-release` credential gate so credential-free blast-radius
  subsets can run without an Anthropic key.
- Whether normalize stays a reduce sub-stage or becomes an independently re-runnable
  top-level phase.
- There are currently **no codex e2e tests**, so the codex/OpenAI-credentials escalation case
  is forward-looking, not present-day.
