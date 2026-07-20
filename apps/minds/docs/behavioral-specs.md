# Behavioral specs

The behavioral-spec corpus at `apps/minds/specs/` describes the externally
observable behavior of minds surfaces as Gherkin `.feature` files: scenarios
for the flows a user or client can take, and rules for the invariants that
hold across all flows and states. Each scenario and rule carries a stable
coordinate that everything outside the corpus uses to refer to it. The corpus
language -- folders, tags, coordinates, invariant scoping, prose sidecars --
is defined by the minds-behavioral-specs skill
(`.claude/skills/minds-behavioral-specs/SKILL.md`); this page covers only the
CLI over the corpus.

## The `minds specs` CLI

Run it from the repo root: the default corpus root (`apps/minds/specs`) is
resolved relative to the current directory, and `--root` points it elsewhere.
`uv run minds specs --help` and each subcommand's `--help` are authoritative
for options and output fields.

Parse every spec file and enforce the corpus language, printing one line per
violation and exiting nonzero if there are any:

```bash
uv run minds specs validate
```

Emit the corpus as JSONL, one record per unit (scenario, scenario outline, or
rule). Each record carries the unit's coordinate, kind, name, location, tags,
steps, parent Rule, and the coordinates of every Rule in scope for it (the
`invariants` field):

```bash
uv run minds specs list
```

The same command takes structural filters, AND-composed: `--area` keeps a
folder subtree, `--unit` a kind, `--tag` an exact raw tag or coordinate, and
`--name`/`--step` case-insensitive substrings:

```bash
uv run minds specs list --area authentication
uv run minds specs list --tag authentication.fresh-code
```

Join the corpus against the `witnesses` markers in the test tree (default
root `apps/minds`; repeat `--tests` to add roots), emitting one record per
unit with its coverage (`full`, `partial`, or `none`) and witnessing tests.
Coverage gaps are data (exit 0); broken links -- a marker naming no unit, or
invalid marker usage -- are errors reported on stderr with a nonzero exit:

```bash
uv run minds specs matrix
```

## Linking tests to specs

A test that verifies a spec unit declares it with the
`witnesses(coordinate, partial=...)` pytest marker; see
[testing-overview.md](./testing-overview.md). `minds specs matrix` reports
how completely the corpus is witnessed by those markers.
