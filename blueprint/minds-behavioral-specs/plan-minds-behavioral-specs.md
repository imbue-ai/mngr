# Plan: minds behavioral specs — language, reference skill, CLI, exemplar

## Overview

- Formalize the minds behavioral-spec language as classic `.feature` files in strict official Gherkin, with `gherkin-official` (the Cucumber reference parser, classic matcher) as the sole validity authority: a file is valid if and only if it parses.
- Capture the formal specification in a new declarative, process-neutral, evergreen skill at `.claude/skills/minds-behavioral-specs/SKILL.md` — the reference that future process skills and tools cite.
- Make identity positional and derived: the first tag on a Scenario/Rule is its identity; its external coordinate is the folder-path qualifier plus the raw tag. Traceability is inverted into test-side `witnesses` markers. Invariants are `Rule:` blocks whose kind and scope come from structure, never spelled in tags.
- Build the minimal `minds specs` CLI (`validate`, `list`, `query`; JSONL output) in this branch so the skill documents real tooling, not aspirations.
- Author the authentication spec corpus fresh in the new language as the in-tree exemplar. The experimental `apps/minds/specs/authentication.md` is deleted; it is not load-bearing and nothing is migrated from it.

## Expected behavior

Language and layout:

- Behavioral specs live under `apps/minds/specs/`, organized into kebab-case area folders. Every folder, the root included, has the same semantics — nothing is special-cased.
- Two reserved basenames per folder: `invariants.feature` (its `Rule:` blocks apply to the folder and everything below) and `overview.md` (prose context for the folder and everything below). Any other `.md` is the sidecar of the same-basename `.feature` in that folder.
- A spec file contains one `Feature:` with an optional description and optional `Background:`; behavior is expressed as tagged `Scenario:` / `Scenario Outline:` (+ `Examples:`) blocks; invariants as `Rule:` blocks (rationale prose as the Rule description, optional illustrating examples as children). `Rule:` blocks inline in an ordinary feature file are file-scoped.
- Tags are kebab-case with no prefixes written in the file. The first tag on a Scenario/Rule is its identity, unique per folder subtree; later tags are auxiliary labels (kebab-case, exempt from uniqueness, no defined semantics yet).
- The external coordinate joins the folder path (relative to the specs root, `/` becomes `.`) with the raw tag: `@fresh-code` in `authentication/signin.feature` is referenced as `authentication.fresh-code`. File basenames never qualify: it is NOT `authentication.signin.fresh-code`. The skill teaches this with exactly this counter-example.
- Tests anywhere in the monorepo declare coverage with `@pytest.mark.witnesses("authentication.fresh-code")`, optionally `partial="what is not covered"`. Spec files carry no test references; the matrix and gap list become derivable (derivation tooling is follow-up work, not this branch).

CLI (`minds specs ...`):

- `validate` — parses all spec files strictly; enforces the language rules (kebab grammar for folders, basenames, and tags; identity-tag presence; subtree uniqueness; reserved-basename collisions, e.g. a `.feature` named `overview`). Located errors, nonzero exit on violation.
- `list` — emits one JSONL record per authored unit (scenario, scenario outline, or rule) carrying coordinate, unit kind, name, file, line, tags, and steps; `--unit` filters.
- `query` — same records, filtered structurally (by tag, name, step text).
- The skill names the subcommands and their purposes only; `minds specs --help` is authoritative for invocation detail.

The skill (`minds-behavioral-specs`):

- Answers, declaratively: where spec files live and how the corpus is organized; how an individual file is structured; the exact dialect ("official Gherkin as accepted by gherkin-official, classic matcher — Feature/Background/Scenario/Scenario Outline/Examples/Rule/Given/When/Then/And/But, doc strings, data tables, comments"); identity and coordinate rules (example plus counter-example); invariant `Rule:` scoping, documented heavily with examples of both file-scoped and subtree-scoped rules; sidecar and overview relationships (positional, by basename/folder — not links); the `witnesses` convention; the CLI by name and purpose.
- Stays evergreen: no authoring/updating/sharding process content; no mention of invariant numbering one way or the other; no flag listings that can drift; refers to living locations (`apps/minds/specs/`, `minds specs --help`) rather than snapshots of their contents.

Exemplar (the authentication corpus, defined on its own terms):

- `apps/minds/specs/authentication/` contains `overview.md`, `invariants.feature`, `signin.feature`, `session.feature`, `landing.feature`, `post-login.feature`, `workspace-bridge.feature`. The experimental `apps/minds/specs/authentication.md` is deleted.
- `overview.md` carries the area's component statement, glossary, and out-of-scope list; each Feature's narrative lives in its own description.
- `witnesses` markers are added to the desktop_client and mngr_forward tests that exercise each behavior, with `partial=` notes where a test covers only part of it; behaviors with no covering test simply have no witnesses (visible once derivation tooling exists). The convention is pytest-only; non-pytest scripts carry no annotations.
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
- New `specs` command group in the minds CLI, following the existing `cli/` module-plus-test pattern, registered alongside `run`/`pool`/`server`/`env`/`paid`.
- New runtime dependency for `apps/minds`: `gherkin-official` (the CLI ships with minds).
- New shared pytest marker `witnesses`, registered in the centralized marker list in `libs/imbue_common` conftest hooks.
- New: the `apps/minds/specs/authentication/` corpus as defined above; the experimental `apps/minds/specs/authentication.md` is deleted, with no migration bookkeeping.
- Annotations: `witnesses` markers added to the desktop_client and mngr_forward tests that exercise the specified behaviors, with `partial=` notes where coverage is incomplete.
- Docs: one short paragraph in `apps/minds/docs/testing-overview.md` naming the `witnesses` convention and pointing at the skill and `apps/minds/specs/`.
- Tests: unit tests for the spec-parsing/validation core and the CLI subcommands; ratchets respected; full suite green before finishing.
- Changelog entries for every touched project: `apps/minds` (update the existing entry), `libs/mngr_forward` (new), `libs/imbue_common` (new), `dev` (new — skill, blueprint plan).
- Out of scope: matrix/gap derivation tooling; process skills that consume this reference; spec content for any area beyond the authentication exemplar.
