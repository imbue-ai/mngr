# Behaviors

The behavior corpus at `apps/minds/behaviors/` describes the externally observable behavior of minds surfaces as Gherkin `.feature` files: scenarios for the flows a user or client can take, and rules for the invariants that hold across all flows and states.
Each scenario and rule carries a stable coordinate that everything outside the corpus uses to refer to it.
The corpus language -- folders, tags, coordinates, invariant scoping, prose sidecars -- is defined by the behaviors skill (`.claude/skills/behaviors/SKILL.md`); this page covers only the CLI as used for the minds corpus.

## The `mngr behaviors` CLI

The CLI (from `libs/mngr_behaviors`) is corpus-generic: it operates on one corpus per invocation, named by a required `--root`.
For the minds corpus, run from the repo root and pass `--root apps/minds/behaviors`.
`uv run mngr behaviors --help` and each subcommand's `--help` are authoritative for options and output fields.

Parse every behavior file and enforce the corpus language, printing one line per violation and exiting nonzero if there are any:

```bash
uv run mngr behaviors validate --root apps/minds/behaviors
```

Emit the corpus as JSONL, one record per unit (scenario, scenario outline, or rule).
Each record carries the unit's coordinate, kind, name, location, tags, steps, parent Rule, and the coordinates of every Rule in scope for it (the `invariants` field):

```bash
uv run mngr behaviors list --root apps/minds/behaviors
```

The same command takes structural filters, AND-composed: `--area` keeps a folder subtree, `--unit` a kind, `--tag` an exact raw tag or coordinate, and `--name`/`--step` case-insensitive substrings:

```bash
uv run mngr behaviors list --root apps/minds/behaviors --area browser-authorization
uv run mngr behaviors list --root apps/minds/behaviors --tag browser-authorization.fresh-code
```

Join the corpus against the `witnesses` markers in its paired test tree (`--tests` defaults to the corpus root's parent -- here `apps/minds`; repeat it to add roots), emitting one record per unit with its coverage (`full`, `partial`, or `none`) and witnessing tests.
Coverage gaps are data (exit 0); broken links -- a marker naming no unit of this corpus, or invalid marker usage -- are errors reported on stderr with a nonzero exit:

```bash
uv run mngr behaviors matrix --root apps/minds/behaviors
```

## Linking tests to behaviors

A test that verifies a behavior unit declares it with the `witnesses(coordinate, partial=...)` pytest marker; see [testing-overview.md](./testing-overview.md).
`mngr behaviors matrix` reports how completely the corpus is witnessed by those markers.
