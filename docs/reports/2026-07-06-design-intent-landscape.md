# Where Design Intent Lives (and Where It Goes to Die)

An audit of how design intent is recorded, enforced, duplicated, and lost in the mngr monorepo, with emphasis on the minds app.

- **Date:** 2026-07-06. A snapshot; trust the citations over the prose, and the code over both.
- **Vantage point:** branch `danver/understanding-minds`.
- **Companion report:** [How Minds Gets Built, Signed, and Shipped](2026-07-06-minds-build-sign-ship.md), which applies this lens to one subsystem in depth.

## The question

Four questions were posed of this repo, and this report answers them in order:

1. What design intent is the team clearly committed to?
2. Where does the implementation diverge from that intent, and where are the divergences localized?
3. Where is intent recorded in a legible format — readily available, parseable, low-token-count?
4. In the absence of a stated meta-rule, enshrine one: **discoverability**, **navigability**, **simplicity**.

"Design intent" here means any artifact that encodes *how the system is supposed to be* — not just architecture docs, but style guides, runbooks, skills, hooks, CI gates, and ratchets. That breadth matters, because this repo's most interesting property is that it treats intent as something you can put on a spectrum of enforcement, and its intent artifacts are healthy in almost exact proportion to where they sit on that spectrum.

## The enforcement ladder

Arrange the repo's intent artifacts by what happens when you violate them:

- **Level 0 — heads.** Intent that exists only in a person. The ToDesktop dashboard access, the minisign key, the `weishi-imbue` release token (`apps/minds/docs/release.md:43` names the account and says nothing else about it). Nothing happens when you violate this; you simply cannot act at all without the person.
- **Level 1 — prose.** `apps/minds/docs/` (17 files), `specs/` (81 entries), `blueprint/` (67 entries), READMEs. Violating prose costs nothing today.
- **Level 2 — convention.** File-naming taxonomies (`_test.py` vs `test_*.py`), folder-per-feature in `specs/`, date-in-branch changelog filenames. Violations produce confusion, eventually.
- **Level 3 — routed procedure.** Skills and justfile recipes: intent packaged as the *thing you run*. The `minds-justfile` skill's rule — "The root justfile is the canonical, auditable, named home for every operational minds task ... If no recipe fits, ADD one" (`.claude/skills/minds-justfile/SKILL.md:8,18-34`) — is itself a meta-intent: it converts ad-hoc operations into named, reviewable artifacts.
- **Level 4 — hooks.** Pre-commit runs ruff, a type checker, a check that the CLI docs are regenerated, and — the charming one — `compile-style-guide`, which extracts the code examples from `style_guide.md` and fails your commit if they stop compiling (`.pre-commit-config.yaml:46-51`). A `PreToolUse` hook physically blocks agents from amending or rebasing commits (`.claude/settings.json:20-29`). The style guide is 1,991 lines of prose, which sounds unenforceable, except part of it literally compiles.
- **Level 5 — CI gates.** `check-changelog` fails any PR missing a changelog entry for each project it touches (`scripts/check_changelog_entries.py`, wired at `.github/workflows/ci.yml:27`); per-project coverage floors; the minds snapshot suite blocking every PR.
- **Level 6 — ratchets.** Each project carries a `test_ratchets.py` where every anti-pattern has a recorded violation count, and the count is only allowed to go down. A ratchet is a covenant: it doesn't demand perfection, it forbids backsliding. Minds currently holds, for example, `time.sleep` at 9, broad exception catches at 17, and mock usage at a hard 0 (`apps/minds/imbue/minds/test_ratchets.py`). Better still, `test_meta_ratchets.py` (744 lines, repo root) enforces that *every project has the same set of ratchets* — a ratchet about ratchets, which is the kind of sentence that tells you the mechanism has been taken seriously.

Here is the finding that organizes everything else in this report: **truthfulness of intent correlates with rung.** Everything at levels 4-6 is true by construction. Level 3 is true where it is exercised — `release.md` has six-plus correction commits, each amending the doc to match reality after a real release, because operators actually run it. Levels 0-1 are where all the rot lives. Every divergence catalogued below is a level-0 or level-1 artifact; not one is a hook, gate, or ratchet.

## The inventory

Where intent lives, what it costs to read, and what keeps it honest. (Answering question 3: the legible, available, low-token places.)

| Artifact | Encodes | ~Lines | Enforcement | Discoverable from cold start? |
|---|---|---|---|---|
| `CLAUDE.md` (root) | Agent operating rules: commands, tests, changelog, git | 159 | Per-rule hooks + CI | Maximal — auto-loaded into every agent context |
| `style_guide.md` (root) | Coding standards; the canonical 4-tier test taxonomy | 1,991 | Pre-commit compiles its examples; ratchets enforce cousins of its rules | Yes — CLAUDE.md says to read it |
| `apps/minds/style_guide.md` | The minds delta (prefer sync) | 5 | None | Easy once in the project; rationale is stale (see below) |
| `apps/minds/README.md` | App overview + doc links | 57 | None | Standard location; accurate |
| `README.md` (root) | mngr's public PyPI face | 405 | Hand-edited source; pre-commit keeps its derived PyPI copy (`libs/mngr/README.md`) in sync | Root; its minds blurb is wrong (see below) |
| `apps/minds/docs/release.md` | The release runbook | 210 | Skill routes to it; exercised every release | Via the `release-minds` skill; not linked from the README |
| `apps/minds/docs/vendor-mngr-sync.md` | The vendor-sync source of truth | 74 | Skills and release.md defer to it | Pointed to; good |
| `apps/minds/docs/` (15 others) | Design, ops, setup, point-in-time audits | 20-462 each | None | **No index file — you browse and guess** |
| `specs/` | Design docs, folder-per-feature (81 entries, roughly half minds-related) | varies | None | **Mentioned nowhere: not in CLAUDE.md, not in the README, no internal README** |
| `blueprint/` | Implementation plans, `plan-*.md` (67 entries, roughly half minds-related) | varies | None | **Same: invisible unless you already know** |
| `justfile` (root) | Every operational entry point, doc-commented, sectioned | 1,061 | Skill mandates its use | `just --list`; good |
| `.claude/skills/` (27) | Procedures for agents | 12-243 each | Social | Surfaced to agents automatically |
| `.pre-commit-config.yaml` | Commit-time gates | ~70 | Hard | Invisible until it fires, which is fine |
| `test_meta_ratchets.py` + 46 per-project ratchet files | Anti-pattern ceilings | 744 + ~400 each | CI | Via style guide's Ratchets section |
| `scripts/check_changelog_entries.py` | The changelog covenant | ~150 | CI | CLAUDE.md explains the rule |
| Per-project `CHANGELOG.md` | History, auto-consolidated nightly | — | Layout ratcheted; entries CI-gated | Standard |
| `ELECTRON_BUNDLING_AUDIT.md` (root) | A 2026-05-18 point-in-time audit | 516 | None | Impossible to miss, unfortunately (see below) |

Two things stand out. First, the *good* end of this table is genuinely good: an agent lands in this repo with CLAUDE.md pre-loaded, which points to the style guide, which defines the test taxonomy and the ratchet system, which are enforced. That chain is discoverable, navigable, and enforced — the META principles below, avant la lettre. Second, the system has a blind spot for *unattached* prose: the ~150 design documents in `specs/` (81 entries) and `blueprint/` (67) — the largest corpus of design intent in the repository, the actual "why" behind features — are referenced by nothing a newcomer would read. They are discoverable only by `ls` at the repo root and curiosity.

## Two patterns worth naming

The skills directory contains a controlled experiment in how to encode procedure, and the two arms came out differently:

**The pointer.** `release-minds/SKILL.md` is twelve lines: "The full procedure lives in `apps/minds/docs/release.md` in the mngr checkout — the single source of truth. This skill only routes you there." One source of truth, one pointer, zero drift surface.

**The duplicator.** `minds-dev-workflow/SKILL.md` is 243 lines that re-encode the vendor-sync command, env-var tables, log paths, and shutdown chain — material that also lives in `vendor-mngr-sync.md`, `environments.md`, and `desktop-app.md`. It is the most detailed skill in the repo and also the largest drift liability, because every fact in it exists somewhere else and nothing reconciles them.

The pointer pattern is the right default, and the repo demonstrably knows it — it just hasn't applied it uniformly.

## Where intent goes to die

The complete list of found divergences between stated intent and reality, each verified against both sides (question 2's first half):

1. **The root audit.** `ELECTRON_BUNDLING_AUDIT.md` announces the packaged app "almost certainly cannot start today." Its two critical findings were fixed weeks later (`apps/minds/scripts/build.js:527-545`, `:557-588`), the app has shipped repeatedly since, and the signed artifact is launch-tested twice daily. The audit also cites a `todesktop.json` that has since been renamed. It sits, undated in-body and unreferenced by anything, at the most discoverable location in the repository.
2. **`overview.md` describes an architecture that was deliberately abandoned.** "Claude Code runs as the main agent process in tmux window 0" (`apps/minds/docs/overview.md:22`) — while `apps/minds/README.md:38` and `design.md:20` explain the actual design: the window-0 command is `sleep infinity && claude`, so Claude *never starts* there. The overview contradicts the two documents next to it.
3. **A removed API haunts three documents.** `POST /api/create-agent` was removed in favor of `/api/v1/workspaces` (`apps/minds/CHANGELOG.md:199`), but `user_story.md:18`, `overview.md:35`, and `design.md:49` still present it as the create surface.
4. **The five-line style guide has a stale premise.** `apps/minds/style_guide.md:5` permits sparing async "since we use FastAPI"; minds migrated to synchronous Flask (`CHANGELOG.md:96`). The conclusion survives its rationale, which is lucky rather than good.
5. **The root README mislabels the product.** `README.md:399` calls minds "an experimental project around scheduling runs of autonomous agents." It is a desktop app for persistent agents; the scheduling project is a different plugin.
6. **CLAUDE.md's release-test claim is false as written.** "Release tests do *not* run in CI" — they run in `.github/workflows/release-tests.yml` on `v*` tags and daily via TMR (`style_guide.md:1858` states this correctly). True meaning: they never gate a PR.
7. **Orphaned test wiring.** `pnpm test:unit`, two renderer-contract Playwright specs, and a `chat-roundtrip.spec.js` mentioned in `test/e2e/README.md` that does not exist. Nothing runs them; nothing notices.
8. **Assorted fossils.** The old provider naming (`CLOUD`, renamed to Vultr) and the removed telegram service linger in `user_story.md` and `workspace/README.md`; `workspace/getting_started.md` (last touched 2026-05-22) predates two architectural migrations.

And the absences — questions with no artifact to answer them (verified by search, not vibes): no index for `apps/minds/docs/`; no production runbook (nothing matches on-call, incident, monitoring, or alerting anywhere in the minds docs); no statement of who owns the ToDesktop account, the Apple identity, or the minisign key; no provider-comparison doc (docker vs Lima vs imbue_cloud vs AWS, cost and when-to-use); no frontend-only dev loop doc; nothing anywhere explaining that `specs/` and `blueprint/` exist or differ.

## Where divergence localizes

Question 2's second half, and the report's central claim. Plotting every item above against the enforcement ladder:

- **All of it is level 0-1.** Narrative overviews, point-in-time audits, README blurbs, a rationale in a five-line delta file, un-run test scripts. Prose whose falseness breaks no build.
- **None of it is level 4-6.** No ratcheted property, hooked rule, or CI gate was found in a divergent state. The changelog layout is ratcheted and correct everywhere; the style guide's examples compile; the ceiling counts hold.
- **Level 3 splits by exercise.** `release.md` is corrected every time someone releases. `overview.md` rots because reading it breaks nothing and nobody's task fails when it lies.

The engineering-culture conclusion writes itself: this team is excellent at enforcement and good at runbooks, and its documentation failures are almost entirely confined to *unexercised prose* — plus a specific bad habit of leaving point-in-time reports (the audit, the incident write-up `destroyed-host-still-listed.md`, the security audit) mixed in with living docs, where they age without any marker of having expired.

## The META rule, enshrined

The following principles were expressed by Danver on 2026-07-06 as governing design intent for how this repository is discovered, documented, and evolved. This section records them as first-class intent — level 1 for now, with a path down the ladder proposed below.

**1. Discoverability.** Intent must be findable from a cold start: at a root, linked from something a newcomer actually reads (README, CLAUDE.md), or auto-loaded. Test: *can an agent with zero context find this in one hop from where it wakes up?* (Counterexample: `specs/`, 81 entries referenced by nothing.)

**2. Navigability.** Artifacts must be self-describing and route onward: each states what it is, who it is for, when it was true, and where the adjacent truths live — in parseable structure (headings, tables, stable paths). Test: *once found, does it take you to the next thing without a human?* (Exemplar: the `release-minds` skill. Counterexample: `apps/minds/docs/` with no index.)

**3. Simplicity.** Low but well-appointed token count: one source of truth per topic plus pointers; every token earning its keep; staleness visible (date-stamp what is point-in-time, delete what is dead). Test: *is anything here a copy, and would a reader know if it had expired?* (Exemplar: `vendor-mngr-sync.md`, 74 lines that four other artifacts defer to. Counterexample: `minds-dev-workflow`'s 244-line re-encoding.)

Corollaries, taken from what already works here: **prefer the pointer to the copy** (the release-minds pattern over the dev-workflow pattern); **push intent down the enforcement ladder** whenever a rule can be a hook, gate, or ratchet instead of a sentence; and **give point-in-time documents a dated home** — which `docs/reports/` (this directory) now is — rather than the repo root.

These were recorded here first and have since been given a durable home in `docs/reports/README.md`. If they earn wider agreement, the remaining promotions are: a three-line entry in `CLAUDE.md` (instant level-something, since agents auto-load it), and eventually a ratchet or CI check for the mechanizable parts (e.g., "every `docs/` directory with more than N files has a README index").

## What I would do next

Prioritized, each small enough for one sitting, none performed as part of this report:

1. **Archive the root audit.** Move `ELECTRON_BUNDLING_AUDIT.md` into `docs/reports/` with a header noting its date and that its critical findings were fixed — or delete it; the git history keeps it either way. Highest legibility return per keystroke available in this repo.
2. **Write `apps/minds/docs/README.md`** — a 30-line index sorting the 17 docs into living (release, environments, vendor-sync, testing, desktop-app) versus point-in-time (audits, incident write-ups), with one line each.
3. **Fix the five mechanical staleness bugs**: overview.md's window-0 claim, the three `/api/create-agent` references, the FastAPI rationale, the root README's minds blurb (edit `README.md` directly — it is the source; `scripts/make_cli_docs.py` propagates it to the PyPI copy at `libs/mngr/README.md`), and CLAUDE.md's release-test sentence (append "on PRs; a dedicated workflow runs them on tags and nightly").
4. **Give `specs/` and `blueprint/` five-line READMEs** ("specs = what/why, blueprint = how; written via the writing-specs / blueprint skills") and one sentence in CLAUDE.md saying they exist.
5. **Slim `minds-dev-workflow` toward the pointer pattern** — keep the orchestration order, link the facts.
6. **Wire or delete the orphan tests**, and fix the `chat-roundtrip.spec.js` reference.
7. **Add a bus-factor note to `release.md`**: who owns the ToDesktop account, the Apple identity, the minisign key, and what happens when `weishi-imbue` is on vacation.
8. **State the tag-namespace design** (`v*` for mngr/PyPI, `minds-v*` for the app, deliberately disjoint) in one sentence in `release.md`.
9. **Schedule the existing `identify-doc-code-disagreements` skill** to run periodically over `apps/minds/docs/` — the repo already owns the tool that finds category-2-through-5 rot; it is simply not pointed at anything on a schedule.

## Unknowns

Whether the CI-polling stop hook that CLAUDE.md references is actually configured (it lives in a marketplace plugin, not in this repo); whether any `specs/`/`blueprint/` entries are abandoned versus merely finished (not assessed per-entry); and the exact contents of the shared ratchet rule-description module (`imbue.imbue_common.ratchet_testing.standard_ratchet_checks`), which is where the per-project ratchet files' rationale actually lives — a mild navigability violation of its own, since CLAUDE.md implies the descriptions sit in each project's file.

## Method

Researched 2026-07-06 by four parallel read-only research agents (build/packaging, CI/signing/release, testing, and a dedicated intent-artifact inventory including per-file git dates), plus one adversarial verification agent that attempted to refute every claim above against the cited files. Absences were verified by search, contradictions by reading both sides. No code was modified.
