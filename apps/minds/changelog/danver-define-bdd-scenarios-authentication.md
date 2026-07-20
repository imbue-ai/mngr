Introduced behavioral specifications for minds: a spec corpus at `apps/minds/specs/`, written in the language defined by the new `minds-behavioral-specs` skill (Gherkin scenarios plus cross-cutting invariants), covering the desktop client's sign-in (one-time login codes), session (signed cookie lifetime and integrity), landing-page routing, post-sign-in destination, and the one-sign-in-opens-every-workspace bridge.

Fixed documentation drift surfaced while writing the spec: `desktop_client/README.md` claimed the session cookie was issued with `Domain=localhost` and that the landing page redirected straight to a sole agent (the cookie is host-only with subdomain access via the forward server's `/goto/` auth bridge, and the landing page lists workspaces even when there is exactly one); a test-helper docstring repeating the stale cookie claim was also corrected.

Added the `minds specs` CLI group (`uv run minds specs ...` from the repo root) over the behavioral-spec corpus at `apps/minds/specs/`, with three subcommands (all taking `--root`, defaulting to the real corpus):

- `validate` parses every `.feature` with `gherkin-official` and enforces the behavioral-spec language rules (English keywords only, kebab-case folders/basenames/tags, an identity tag on every unit, unique coordinate claims including Feature/Examples block tags, reserved `overview`/`invariants` filenames, no dangling `.md` sidecars or foreign files), printing every violation with file and line and exiting nonzero on any.

- `list` emits one JSONL record per authored unit (scenario, scenario-outline, or rule) carrying coordinate, kind, name, file, line, tags, steps, and the parent Rule's coordinate for nested units; `--unit` filters by kind. Stdout is pure JSONL; problems that omit units from the listing go to stderr with a nonzero exit.

- `query` emits the same records filtered by `--tag` (exact raw tag or coordinate), `--name`, and `--step` (case-insensitive substrings), combined as AND.

The scanning/validation engine lives in `imbue.minds.core.behavioral_specs` and takes the corpus root as a parameter for testability. Adds the `gherkin-official` dependency (the Cucumber reference parser, the arbiter of spec syntax).

The corpus itself: `apps/minds/specs/authentication/` holds `overview.md` (the area's component statement, glossary, and out-of-scope list) plus five feature files -- `signin.feature`, `session.feature`, `landing.feature`, `post-login.feature`, `workspace-bridge.feature` (24 tagged scenarios/outlines) -- and `invariants.feature`, whose eight subtree-scoped Rules carry the cross-cutting invariants. Every unit's coordinate is `authentication.<tag>` (e.g. `authentication.fresh-code`). It re-expresses the essence of an interim proof-of-concept document (`specs/authentication.md`, created and replaced within this PR); test-to-spec links arrive as `witnesses` markers in a separate PR.

Added a unit test (`behavioral_specs/corpus_test.py::test_live_corpus_has_no_violations`) asserting that the live corpus at `apps/minds/specs/` always satisfies the spec-language rules.

Added documentation: `docs/behavioral-specs.md` (the corpus and the `minds specs` CLI, linked from the README), and a `testing-overview.md` paragraph describing the `witnesses(coordinate, partial=...)` marker convention for linking tests to spec units.
