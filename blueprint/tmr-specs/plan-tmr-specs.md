# Plan: `mngr tmr-specs` — spec-anchored test map-reduce

## Overview

- Build a second map-reduce recipe in `libs/mngr_tmr`, sibling to `TestMapReduceRecipe`: one mapper agent per behavioral-spec `.feature` file, each making the tests that witness that file's spec units converge to the units' scope; a reducer integrates and normalizes, exactly as TMR does today.
- The spec source is `mngr specs` (the `libs/mngr_specs` library, a fixed layer-1 interface): `scan_corpus` at discovery, the `mngr specs` CLI on agent hosts for self-serve detail and verification. The caller varies only the corpus root and prompt/justfile variant choices.
- The anchor generalizes TMR's lineage (tutorial block -> docstring -> spec unit): the unit's steps plus its in-scope invariant Rules define scope; forward, every observable claim is verified by some witnessing test; backward, every assertion traces to a step or an in-scope invariant, else it is gold-plating to remove. Creation is not a mode — an unwitnessed unit is the far-from-fixed-point case of the same objective.
- Key settled decisions:
  - Placement/name: recipe modules live in `libs/mngr_tmr` as clearly-separated `spec_*` files; top-level command `mngr tmr-specs`; `mngr_tmr` gains the `imbue-mngr-specs` dependency; `libs/mngr_specs` is untouched (its PyPI trajectory stays clean).
  - Fan-out: per-`.feature`-file (flexible by design — outcomes are keyed per coordinate, so re-partitioning later touches only `discover()`'s grouping and task ids). `invariants.feature` is a task like any other; standalone Rules get direct witnesses.
  - Outcome schema: change kinds `{CREATE_TEST, IMPROVE_TEST, FIX_TEST, FIX_IMPL}` (no spec-edit kind exists, structurally) plus per-coordinate verdict records with decomplected axes: `coverage` (matrix vocabulary), `steady` (honest-partial: true only when `partial=` notes name residue untestable in kind), `witnesses`, `blockers`, `spec_problems` (the only corpus-change channel — proposed edits ride the outcome JSON, never the tree).
  - Read-only corpus enforcement: prompt instruction (L1) plus a Python egress gate in `on_reducer_finalized` (L3) that refuses to emit a reducer branch whose diff touches the corpus root. Corpus `validate` fail-fasts in `discover()`; the matrix link-check runs at normalize. No `should_pull` corpus clause.
  - v1 discovery is `scan_corpus` only — no witness harvesting up front; mappers self-collect on-host; the report's verified coverage comes from the reducer's normalize-stage `mngr specs matrix` artifact.
  - Conceptual integrity: task ids are root-relative `.feature` paths; `display_id`s are folder-qualified dotted names (`authentication.signin`) mirroring coordinate style; outcome filenames reuse TMR's (`testing_agent_outcome.json` / `integrator_outcome.json`).
- Out of scope, explicitly: CI workflow wiring (`tmr.yml`); the real fleet run (happens on a subsequently stacked branch); any change to `apps/minds/specs/`, the behavioral-specs skill, or the layer-1 CLI.

## Expected behavior

- `uv run mngr tmr-specs --root apps/minds/specs [--tests PATH ...] [--area X] [--tag X] [--unit KIND] [--name SLUG] [--mapper-prompt F] [--reducer-prompt F] [framework opts] [-- flags]` runs a full map-reduce over the corpus at `--root`.
- Discovery validates the corpus with `scan_corpus` and aborts (before any agent launches) if there are violations or zero units; `--area`/`--tag`/`--unit` filter units (AND-composed, selection-only, `mngr specs list` semantics); remaining units group by `.feature` file into tasks in corpus order. An empty post-filter selection is an error.
- One mapper per feature file receives a lean prompt: the file path, a table of its units (coordinate, kind, name, line, parent, in-scope invariant coordinates), `mngr specs` self-serve commands, and the contract (spec-as-anchor forward/backward convergence, witnesses-marker honesty with `partial=`/steady semantics, corpus read-only, `FIX_IMPL` vs `BLOCKED`+`spec_problems` arbitration, commit discipline `[KIND] coords: summary`, outcome JSON schema). The mapper reads `.feature` files and sidecars on-host, runs the specific witness tests it touches, and publishes outcome + branch bundle like TMR.
- "No change needed" (empty `changes`, all units steady) and deletion of gold-plating (credited under `IMPROVE_TEST`) are first-class outcomes. Every touched test carries correct `witnesses` markers with `partial=` set or cleared honestly.
- The reducer integrates exactly as TMR (should_pull predicate unchanged; squash `CREATE_TEST`/`IMPROVE_TEST`/`FIX_TEST` into one commit; cherry-pick `FIX_IMPL` individually by priority), then normalizes: dedupes fixtures/scaffolding parallel mappers created independently (two-sided value check; never a unit's observed behavior itself), triages `FIXME(tmr-specs)` blockers into normalizations/escalations, runs `mngr specs matrix` on the integrated tree, and ships `matrix.jsonl` in its outputs.
- On the operator machine, `on_reducer_finalized` applies the reducer bundle, then runs the egress gate: if the integrated branch's diff touches the corpus root, the reducer-branch event is NOT emitted and the report shows a loud violation banner instead. Otherwise the branch event is emitted as today.
- The HTML report shows TMR-style task rows plus two new sections: a per-coordinate coverage matrix (claimed by mappers -> verified by the reducer's matrix artifact, with steady flags, witnesses, partial notes, blockers) and a spec-escalations section aggregating `spec_problems` with proposed edits. Report renders incrementally during the run and uploads via the existing S3 helper.
- `just tmr-specs-minds [args]` invokes the minds variant: `--root apps/minds/specs --name tmr-specs-minds --mapper-prompt apps/minds/tmr/specs_mapper.j2`, no testing flags after `--` (spec mappers are node-id-directed; host-capability guardrails live in the variant prompt's blockers section).
- Existing `mngr tmr` behavior is unchanged.

## Implementation plan

### Shared-helper extraction (mechanical, keeps TMR green)

- `libs/mngr_tmr/imbue/mngr_tmr/recipe_common.py` (new): move from `recipe.py` the recipe-name pattern + validation (`RECIPE_NAME_PATTERN`, `validate_recipe_name`, `InvalidRecipeNameError`), `apply_branch_bundle` (was `_apply_branch_bundle`), the branch-bundle filename constant, and the reducer-branch event emit helper; move from `cli.py` the `--` split command class (`_TmrCommand` -> `SplitTestingFlagsCommand`). `recipe.py`/`cli.py` import from it.
- `libs/mngr_tmr/imbue/mngr_tmr/prompts.py` (modify): promote `_resolve_template` -> `resolve_template` and `_PUBLISH_OUTPUTS_SNIPPET` -> `PUBLISH_OUTPUTS_SNIPPET` (now two consumers); parameterize the loader package/dir where needed. Outcome filename constants become importable.

### Recipe

- `libs/mngr_tmr/imbue/mngr_tmr/spec_recipe.py` (new):
  - `SpecMapReduceRecipe(MapReduceRecipe, FrozenModel)` with fields: `name` (default `"tmr-specs"`, validated via `recipe_common`), `corpus_root: Path`, `test_roots: tuple[Path, ...]` (empty -> effective default `corpus_root.parent`), `area: str | None`, `tag: str | None`, `unit_kind: SpecUnitKind | None`, `testing_flags: tuple[str, ...]`, `mapper_prompt_path: Path | None`, `reducer_prompt_path: Path | None`.
  - `discover(ctx)`: `scan_corpus(ctx.source_dir / corpus_root)`; violations -> raise `SpecCorpusInvalidError` (fail-fast, listing them); apply filters (reuse `mngr_specs` filter helpers if public, else a small local `_unit_matches_filters`); group units by root-relative file -> `MapReduceTask(id="authentication/signin.feature", display_id="authentication.signin")`; empty selection -> `NoSpecUnitsError`.
  - `build_mapper_prompt(ctx, task)`: re-scan corpus (stateless-recipe pattern; corpus parse is milliseconds), select the task file's units, compute `binding_invariant_coordinates` per unit plus a coordinate->Rule-name context table, delegate to `build_spec_mapper_prompt`.
  - `build_reducer_prompt(ctx)`: delegate to `build_spec_reducer_prompt` (corpus root, effective test roots, template override).
  - `on_mapper_finalized`: `apply_branch_bundle` unconditionally (TMR parity).
  - `on_reducer_finalized`: apply bundle; egress gate: `git merge-base HEAD <branch>` then `git diff --quiet <mb>..<branch> -- <corpus_root>`; clean -> emit reducer-branch event; dirty -> write `corpus_violation.json` (offending paths) into the reducer agent dir and do not emit.
  - `render_report`: delegate to `generate_spec_html_report` + `maybe_upload_report`.

### Outcome models and report

- `libs/mngr_tmr/imbue/mngr_tmr/spec_report.py` (new):
  - `SpecChangeKind(UpperCaseStrEnum)`: `CREATE_TEST, IMPROVE_TEST, FIX_TEST, FIX_IMPL`. Reuse `ChangeStatus`/`Change`/`ReportSection`/`IntegratorResult`/`TestRunInfo` from `report.py` (promote to importable where private).
  - `WitnessClaim(FrozenModel)`: `node_id`, `partial: str | None`.
  - `SpecProblem(FrozenModel)`: `problem`, `proposed_edit`.
  - `UnitVerdict(FrozenModel)`: `coordinate`, `coverage: SpecCoverage` (imported from `mngr_specs`), `steady: bool`, `witnesses: tuple[WitnessClaim, ...]`, `blockers: tuple[str, ...]`, `spec_problems: tuple[SpecProblem, ...]`, `summary_markdown`. Validators: `coverage == NONE` forbids `steady=True`; `coverage == FULL` requires `steady=True`.
  - `SpecTaskResult(FrozenModel)`: `changes: dict[SpecChangeKind, Change]`, `units: tuple[UnitVerdict, ...]`, `errored`, `tests_passing_before/after: bool | None`, `test_runs`, `summary_markdown`; parser mirroring `_parse_outcome_json`.
  - Reducer outcome: TMR's `IntegratorResult` schema unchanged; verified coverage is read from the reducer's `matrix.jsonl` artifact when present (parse with a thin loader; absent artifact -> "verified" column shows pending/unavailable).
  - `generate_spec_html_report(...)`: renders `spec_report_assets/spec_report.html.j2`; section derivation reuses `ReportSection` (all three test kinds -> non-impl bucket); static assets (`report.css`, `artifacts.js`) shared from the existing `report_assets` package.
- `libs/mngr_tmr/imbue/mngr_tmr/spec_report_assets/spec_report.html.j2` (new): TMR-style task rows + the coverage-matrix section (coordinate, kind, claimed, verified, steady, witnesses with partial notes, blockers) + the spec-escalations section (aggregated `spec_problems` with proposed edits) + normalizations/escalations panels + the corpus-violation banner.

### Prompts

- `libs/mngr_tmr/imbue/mngr_tmr/spec_prompts.py` (new): `build_spec_mapper_prompt(...)` / `build_spec_reducer_prompt(...)` rendering the packaged templates via `resolve_template` (ChoiceLoader override support, identical to TMR); context includes corpus root, feature path, unit table, in-scope invariants, `mngr specs` command strings, testing flags, outcome filenames, publish snippet.
- `libs/mngr_tmr/imbue/mngr_tmr/prompt_assets/spec_mapper.j2` (new): the generic mapper contract — spec unit as anchor; corpus READ-ONLY with no exceptions; forward/backward convergence with invariants as legitimate trace targets; witnesses-marker honesty (`partial=` set/cleared; steady = residue untestable in kind, e.g. universally quantified interleavings — expensive-but-testable is not steady); creation as the same objective; generic placement frame ("follow the target project's test taxonomy"); `FIXME(tmr-specs):` channel for cross-cutting blockers; one commit per kind, `[KIND] coords: summary`; outcome JSON schema (changes + units[] + errored + tests_passing_* + test_runs + summary); publish snippet; no-user-input.
- `libs/mngr_tmr/imbue/mngr_tmr/prompt_assets/spec_reducer.j2` (new): discover/fetch qualifying branches (TMR's should_pull verbatim); cherry-pick strategy (squash test kinds, `FIX_IMPL` by priority); normalize — scaffolding dedup with the two-sided value check, `FIXME(tmr-specs)` triage into normalizations/escalations, `mngr specs matrix --root ... --tests ... > matrix.jsonl` (link errors are failures to fix or escalate), blast-radius test verification; outcome JSON (IntegratorResult schema); publish snippet including `matrix.jsonl`.

### CLI and plugin

- `libs/mngr_tmr/imbue/mngr_tmr/spec_cli.py` (new): `SpecTmrCliOptions(MapReduceCliOptions)` (`name`, `mapper_prompt`, `reducer_prompt`, `root`, `tests`, `area`, `tag`, `unit_kind`, `testing_flags`); `tmr_specs` click command (cls=`SplitTestingFlagsCommand`) stacking recipe options + `add_mapreduce_options` + `add_common_options`; `--root` required `click.Path(exists=True, file_okay=False)`; `--tests` `multiple=True`; builds `SpecMapReduceRecipe`, calls `run_mapreduce`.
- `libs/mngr_tmr/imbue/mngr_tmr/plugin.py` (modify): `register_cli_commands` returns `[tmr, tmr_specs]`.
- `libs/mngr_tmr/pyproject.toml` (modify): add `imbue-mngr-specs==0.1.0` dependency + `[tool.uv.sources]` workspace entry.
- CLI docs: regenerate via `uv run python scripts/make_cli_docs.py` (adds `libs/mngr/docs/commands/.../tmr-specs.md`; add the command to the script's list if it enumerates explicitly).

### Minds variant and justfile

- `apps/minds/tmr/specs_mapper.j2` (new): self-contained variant (per existing convention) — minds preamble; the placement frame (witnesses default to integration `test_*.py`; release when the flow needs the full stack; unit `_test.py` when observable in-process; acceptance reserved for core flows); minds infra blockers section (Docker daemon, secrets, `minds_deployment`/`minds_services`/`minds_snapshot_resume` host capabilities) mirroring the existing minds mapper variant; same contract/schema/publish body as the packaged template.
- `justfile` (modify): add `tmr-specs-minds *args:` -> `uv run --project libs/mngr_tmr mngr tmr-specs --root apps/minds/specs --name tmr-specs-minds --mapper-prompt apps/minds/tmr/specs_mapper.j2 {{args}}` (no `--` testing flags), next to `tmr-minds` with a comment.
- `libs/mngr_tmr/README.md` (modify): document the second recipe and its variant flags under the existing Variants section.

### Changelogs (one per touched project)

- `libs/mngr_tmr/changelog/danver-minds-specs-mapreduce-fable.md` — the recipe, CLI, prompts, report, egress gate.
- `apps/minds/changelog/danver-minds-specs-mapreduce-fable.md` — the minds variant prompt.
- `libs/mngr/changelog/danver-minds-specs-mapreduce-fable.md` — regenerated CLI docs page.
- `dev/changelog/danver-minds-specs-mapreduce-fable.md` — the justfile recipe and this blueprint.

## Implementation phases

1. **Shared-helper extraction**: create `recipe_common.py`, promote `prompts.py`/`report.py` privates that gain a second consumer; update TMR imports; full `libs/mngr_tmr` tests stay green. (TDD: existing tests are the harness.)
2. **Recipe core**: `spec_recipe.py` discovery — corpus fail-fast, filters, per-file grouping, dotted display ids — with unit tests against fixture corpora and one test against the live `apps/minds/specs` (6 tasks today, `authentication.invariants` among them).
3. **Outcome models**: `spec_report.py` models + parsers + validators, unit-tested (steady/coverage invariants, malformed-JSON handling).
4. **Prompts**: packaged `spec_mapper.j2`/`spec_reducer.j2` + `spec_prompts.py` builders; tests assert contract passages (read-only corpus, forward/backward, steady semantics), context rendering, and override/`{% extends %}` resolution.
5. **CLI + plugin**: `spec_cli.py`, plugin registration, `--` split, option threading into the recipe; regenerate CLI docs; tests mirror `cli_test.py`/`plugin_test.py`.
6. **Egress gate + report**: `on_reducer_finalized` gate with temp-git-repo tests (clean branch -> event emitted; corpus-touching branch -> no event + `corpus_violation.json`); `generate_spec_html_report` + template with tests (coverage matrix, escalations, violation banner, matrix.jsonl join).
7. **Minds variant + wiring**: `apps/minds/tmr/specs_mapper.j2` (render test, parity with the existing committed-variant test), justfile recipe, README, changelogs.
8. **Finish line**: ratchet snapshot trims where counts drop; `just test-offload` full suite green; manual verification (`uv run mngr tmr-specs --help`, `just -n tmr-specs-minds`, prompt-render spot-check); commit; draft PR against `danver/define-bdd-scenarios-authentication` once pushing is authorized.

## Testing strategy

- **Unit tests (`*_test.py`, colocated in `libs/mngr_tmr`)**:
  - `spec_recipe_test.py`: name validation; discovery grouping, ordering, and dotted display ids; each filter (`area` segment-matching, `tag` raw-vs-coordinate, `unit` kind) and their AND-composition; fail-fast on corpus violations and on empty selection; live-corpus discovery snapshot; egress gate both ways in a temp git repo.
  - `spec_report_test.py`: enum/model round-trips; `UnitVerdict` validators; outcome parsing (valid, missing fields, malformed); section derivation; HTML generation (rows, coverage matrix claimed/verified columns, steady flags, spec-escalations, violation banner, HTML escaping).
  - `spec_prompts_test.py`: mapper prompt contains file path, coordinates, invariant table, self-serve commands, read-only language, steady wording; reducer prompt contains should_pull, matrix command, `matrix.jsonl` publish; override template resolution incl. `{% extends %}`; the committed `apps/minds/tmr/specs_mapper.j2` renders.
  - `spec_cli_test.py` + `plugin_test.py` update: two commands registered; help surface; `--` split; `--tests` repeatability; option->recipe threading.
- **Fixtures**: reuse `imbue.mngr_specs.testing` corpus builders and existing shared conftest fixtures; no new test-utility tests.
- **Ratchets**: honor in spirit; trim inline snapshots where counts drop; no evasions.
- **Edge cases covered**: corpus with violations; empty corpus; filters selecting nothing; a task file with only Rules (`invariants.feature`); reducer outcome without `matrix.jsonl`; corpus-touching reducer branch; `coverage=none, steady=true` rejected.
- **Explicitly not tested here**: real fleet runs (stacked branch), tmux/interactive flows, CI workflow wiring.
- **Full-suite gate**: `just test-offload` green before the branch is done; acceptance tests run in CI.

## Open questions

- Minds variant blocker wording will be tuned empirically during the stacked-branch real run; the initial section mirrors the existing minds TMR variant.
- Whether `--reintegrate` needs any spec-recipe-specific handling beyond the framework's (expected: none; verify during the real run).
- Report visual polish (column widths, matrix grouping by area) once real artifacts exist.
- Whether `mngr_specs` exposes its list filters as public helpers; if not, the local `_unit_matches_filters` stays (small, semantics documented by `mngr specs list --help`).
