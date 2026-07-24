Add `mngr tmr-specs`, a second map-reduce recipe anchored on behavioral specs: it scans a corpus (via the `imbue-mngr-specs` library, new dependency), fans out one agent per `.feature` file, and each agent creates or updates the tests witnessing that file's units (scenarios, scenario outlines, and invariant Rules), keeping `witnesses(coordinate, partial=...)` markers honest.

The mapper outcome schema is two-layered: TMR-style `changes` keyed by kind (`CREATE_TEST`, `IMPROVE_TEST`, `FIX_TEST`, `FIX_IMPL` -- there is deliberately no spec-edit kind) plus per-coordinate verdicts (`FULL`, `PARTIAL_STEADY`, `PARTIAL_IMPROVABLE`, `NONE`) with witnesses, blockers, and spec-problem escalations. `PARTIAL_STEADY` marks the honest-partial fixed point: residue untestable in kind.

The corpus is read-only to the whole pipeline: prompts state it, and a mechanical egress gate in `on_reducer_finalized` refuses to emit an integrated branch whose diff touches the corpus root. Spec edits can only be *proposed*, via the report's spec-escalations section.

The reducer integrates exactly as TMR (should-pull, squash the test kinds, cherry-pick `FIX_IMPL` by priority), then dedupes fixtures parallel mappers created independently and audits witness links by running `mngr specs matrix` over the integrated tree, shipping `matrix.jsonl` so the HTML report can show claimed-vs-verified coverage per coordinate.

The packaged `spec_mapper.j2` defines two named block slots (`project_guidance`, `infra_blockers`) that a variant fills via `{% extends %}` instead of forking the contract body; the spec report renders agent markdown with raw HTML disabled.

Shared helpers that gained a second consumer were promoted to public names (`apply_branch_bundle`, `resolve_template`, `SplitTestingFlagsCommand`, report section constants, etc.); `mngr tmr` behavior is unchanged.
