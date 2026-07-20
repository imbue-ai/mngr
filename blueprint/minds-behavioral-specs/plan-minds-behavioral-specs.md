# Plan: minds behavioral specs — language, reference skill, CLI, exemplar

## Overview

- Formalize the minds behavioral-spec language as classic `.feature` files in strict official Gherkin, with `gherkin-official` (the Cucumber reference parser, classic matcher) as the sole validity authority: a file is valid if and only if it parses.
- Capture the formal specification in a new declarative, process-neutral, evergreen skill at `.claude/skills/minds-behavioral-specs/SKILL.md` — the reference that future process skills and tools cite.
- Make identity positional and derived: the first tag on a Scenario/Rule is its identity; its external coordinate is the folder-path qualifier plus the raw tag — the stable handle by which anything outside the spec refers in. Invariants are `Rule:` blocks whose kind and scope come from structure, never spelled in tags.
- Ship the `witnesses` back-link convention: tests name the coordinates they verify via a pytest marker, registered and documented in this branch. Filling in the authentication corpus's own annotations is a separate PR; the skill treats back-linking as the normal expectation without remarking on current annotation state.
- Build the minimal `minds specs` CLI (`validate`, `list`, `query`; JSONL output) in this branch so the skill documents real tooling, not aspirations.
- Deliver `apps/minds/specs/authentication/` as the in-tree exemplar: the conceptual contents of `apps/minds/specs/authentication.md` — essence, not accidents — re-expressed in the newly defined language. A Fable subagent authors it once the skill exists (the first sufficiency test of the skill); the old file is then deleted, replaced by its re-expression.

## Expected behavior

Language and layout:

- Behavioral specs live under `apps/minds/specs/`, organized into kebab-case area folders. Every folder, the root included, has the same semantics — nothing is special-cased.
- Two reserved basenames per folder: `invariants.feature` (its `Rule:` blocks apply to the folder and everything below) and `overview.md` (prose context for the folder and everything below). Any other `.md` is the sidecar of the same-basename `.feature` in that folder.
- A spec file contains one `Feature:` with an optional description and optional `Background:`; behavior is expressed as tagged `Scenario:` / `Scenario Outline:` (+ `Examples:`) blocks; invariants as `Rule:` blocks (rationale prose as the Rule description, optional illustrating examples as children). `Rule:` blocks inline in an ordinary feature file are file-scoped.
- Tags are kebab-case with no prefixes written in the file. The first tag on a Scenario/Scenario Outline/Rule is its identity; later tags are auxiliary labels (kebab-case, exempt from uniqueness, no defined semantics yet). Tags on `Feature`/`Examples` blocks are permitted, semantics-free, and claim coordinates. Uniqueness is coordinate uniqueness: no coordinate is claimed twice, i.e. claiming tags are unique within their folder; nested folders may reuse raw names.
- The external coordinate joins the folder path (relative to the specs root, `/` becomes `.`) with the raw tag: `@fresh-code` in `authentication/signin.feature` is referenced as `authentication.fresh-code`. File basenames never qualify: it is NOT `authentication.signin.fresh-code`. The skill teaches this with exactly this counter-example.
- Tests back-link to the units they verify: `@pytest.mark.witnesses("authentication.fresh-code")`, optionally `partial="what is not covered"`; the marker is registered in the shared pytest settings and usable monorepo-wide. The direction is one-way — spec files never reference tests. Matrix/gap derivation tooling remains follow-up work.

CLI (`minds specs ...`):

- `validate` — parses all spec files strictly; enforces the language rules (kebab grammar for folders, basenames, and tags; English keywords only; identity-tag presence; coordinate uniqueness including Feature/Examples tag claims; reserved-name shadowing, e.g. a `.feature` named `overview`; dangling `.md` files). Located errors, nonzero exit on violation.
- `list` — emits one JSONL record per authored unit (scenario, scenario outline, or rule) carrying coordinate, unit kind, name, file, line, tags, and steps; `--unit` filters.
- `query` — same records, filtered structurally (by tag, name, step text).
- The skill names the subcommands and their purposes only; `minds specs --help` is authoritative for invocation detail.

The skill (`minds-behavioral-specs`):

- Answers, declaratively: where spec files live and how the corpus is organized; how an individual file is structured; the exact dialect ("official Gherkin as accepted by gherkin-official, classic matcher — Feature/Background/Scenario/Scenario Outline/Examples/Rule/Given/When/Then/And/But, doc strings, data tables, comments"); identity and coordinate rules (example plus counter-example); invariant `Rule:` scoping, documented heavily with examples of both file-scoped and subtree-scoped rules; sidecar and overview relationships (positional, by basename/folder — not links); the `witnesses` back-link convention as the normal expectation; the CLI by name and purpose.
- Stays evergreen: no authoring/updating/sharding process content; no mention of invariant numbering one way or the other; no flag listings that can drift; refers to living locations (`apps/minds/specs/`, `minds specs --help`) rather than snapshots of their contents.

Exemplar (the authentication corpus — the conceptual contents of `authentication.md`, re-expressed):

- Authored by a Fable subagent as soon as the skill exists, from two inputs: the skill (the language) and `authentication.md` (the concepts — essence, not accidents). It validates its output with the tooling; `authentication.md` is deleted once replaced.
- The subagent grounds the corpus against the existing tests and code behavior — judgment-based verification only. It does NOT add test-to-spec backlinks of any kind.
- `apps/minds/specs/authentication/` contains `overview.md`, `invariants.feature`, `signin.feature`, `session.feature`, `landing.feature`, `post-login.feature`, `workspace-bridge.feature`.
- `overview.md` carries the area's component statement, glossary, and out-of-scope list; each Feature's narrative lives in its own description.
- The corpus (file, Feature, identity tags; every coordinate is `authentication.<tag>`):

| File | Feature | Identity tags |
|---|---|---|
| signin.feature | Sign-in with a one-time login code | @fresh-code, @used-code, @unknown-code, @prefetch, @already-signed-in, @missing-code |
| session.feature | Session lifetime and integrity | @survives-restart, @tampered-token, @foreign-token, @expired-token |
| landing.feature | Landing page routing | @signed-out-home, @consent-gate, @discovering, @empty-shows-create-form, @deep-link-prefill, @lists-workspaces |
| post-login.feature | Post-sign-in destination | @signed-out-arrival, @consent-first, @safe-return-to, @default-destination |
| workspace-bridge.feature | One sign-in opens every workspace | @open-from-landing, @direct-navigation, @signed-out-workspace, @non-html-refused |
| invariants.feature | (Rules, subtree-scoped) | @single-use-codes, @no-data-without-session, @sessions-unforgeable, @signing-key-minted-once, @no-open-redirects, @single-credential, @fetch-never-spends, @credential-not-forwarded |

## Changes

- New skill file: `.claude/skills/minds-behavioral-specs/SKILL.md` (single file, frontmatter per repo skill convention).
- New `specs` command group in the minds CLI, following the existing `cli/` module-plus-test pattern, registered alongside `run`/`pool`/`server`/`env`/`paid`; simple, limited CLI documentation in the standard, discoverable locations.
- New runtime dependency for `apps/minds`: `gherkin-official` (the CLI ships with minds).
- New shared pytest marker `witnesses(coordinate, partial=...)`, registered in the centralized marker list in `libs/imbue_common` conftest hooks.
- New: the `apps/minds/specs/authentication/` corpus as defined above, authored by a Fable subagent from the skill plus the conceptual contents of `authentication.md` (essence, not accidents); `authentication.md` is deleted once replaced by its re-expression.
- Docs: one short paragraph in `apps/minds/docs/testing-overview.md` naming the `witnesses` convention and pointing at the skill and `apps/minds/specs/`.
- Tests: unit tests for the spec-parsing/validation core and the CLI subcommands (built test-first); ratchets respected; full suite green before finishing.
- Changelog entries for every touched project: `apps/minds` (update the existing entry), `libs/imbue_common` (new), `dev` (new — skill, blueprint plan).
- Out of scope: annotating the authentication corpus's tests with `witnesses` markers (a separate PR); matrix/gap derivation tooling; process skills that consume this reference; spec content for any area beyond the authentication exemplar.

## Execution

Deliverable 1 — Skill:

1. Write a clean, clear skill capturing the decisions and intent above.
2. Delegate a Fable subagent to review it: apply /de-complect and the spirit of /crispy-comments, as an outside perspective that the phrasing introduces no scars, and highlight problems.
3. Critically evaluate and incorporate the feedback; judgment calls go to the user.

Deliverable 2 — CLI tool:

1. Delegate a subagent to build the `minds specs` CLI with the features described above, using test-driven development.
2. Delegate a subagent to write simple, limited documentation for the CLI in the standard, discoverable locations.

Deliverable 3 — Feature migration:

1. Delegate a Fable subagent to read the skill and the now-obsolete `authentication.md`, complete the migration into `apps/minds/specs/authentication/`, and ground it against the existing tests and code behavior (judgment-based verification only). It does not annotate any tests — the corpus's `witnesses` backlinks are a separate PR.

Cross-cutting:

- Register the `witnesses` marker in the shared pytest settings (`libs/imbue_common`) so back-linking works monorepo-wide from this branch onward.
- Changelog entries, testing-overview paragraph, full test suite, draft PR.
