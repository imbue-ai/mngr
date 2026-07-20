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
rule):

```bash
uv run minds specs list
```

Emit the same records, structurally filtered -- by tag or coordinate, by name
substring, or by step-text substring:

```bash
uv run minds specs query --tag authentication.fresh-code
```

## Linking tests to specs

A test that verifies a spec unit declares it with the
`witnesses(coordinate, partial=...)` pytest marker; see
[testing-overview.md](./testing-overview.md).
