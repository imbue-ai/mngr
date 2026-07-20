# Behavioral specs

The behavioral-spec corpus at `apps/minds/specs/` describes the externally
observable behavior of minds surfaces as Gherkin `.feature` files: scenarios
for the flows a user or client can take, and rules for the invariants that
hold across all flows and states. Each scenario and rule carries a stable
coordinate that everything outside the corpus uses to refer to it. The corpus
language -- folders, tags, coordinates, invariant scoping, prose sidecars --
is defined by the behavioral-specs skill
(`.claude/skills/behavioral-specs/SKILL.md`); this page covers only the CLI
as used for the minds corpus.

## The `mngr specs` CLI

The CLI (from `libs/mngr_specs`) is corpus-generic: it operates on one corpus
per invocation, named by a required `--root`. For the minds corpus, run from
the repo root and pass `--root apps/minds/specs`. `uv run mngr specs --help`
and each subcommand's `--help` are authoritative for options and output
fields.

Parse every spec file and enforce the corpus language, printing one line per
violation and exiting nonzero if there are any:

```bash
uv run mngr specs validate --root apps/minds/specs
```

Emit the corpus as JSONL, one record per unit (scenario, scenario outline, or
rule). Each record carries the unit's coordinate, kind, name, location, tags,
steps, parent Rule, and the coordinates of every Rule in scope for it (the
`invariants` field):

```bash
uv run mngr specs list --root apps/minds/specs
```

The same command takes structural filters, AND-composed: `--area` keeps a
folder subtree, `--unit` a kind, `--tag` an exact raw tag or coordinate, and
`--name`/`--step` case-insensitive substrings:

```bash
uv run mngr specs list --root apps/minds/specs --area authentication
uv run mngr specs list --root apps/minds/specs --tag authentication.fresh-code
```

Join the corpus against the `witnesses` markers in its paired test tree
(`--tests` defaults to the corpus root's parent -- here `apps/minds`; repeat
it to add roots), emitting one record per unit with its coverage (`full`,
`partial`, or `none`) and witnessing tests. Coverage gaps are data (exit 0);
broken links -- a marker naming no unit of this corpus, or invalid marker
usage -- are errors reported on stderr with a nonzero exit:

```bash
uv run mngr specs matrix --root apps/minds/specs
```

## Linking tests to specs

A test that verifies a spec unit declares it with the
`witnesses(coordinate, partial=...)` pytest marker; see
[testing-overview.md](./testing-overview.md). `mngr specs matrix` reports
how completely the corpus is witnessed by those markers.
