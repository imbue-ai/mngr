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

Emit enriched unit records -- everything a test-writing consumer needs around
each unit: effective steps (Background folded in), Examples rows for Scenario
Outlines, Feature/Rule descriptions, the relevant prose (folder overviews plus
the file's sidecar), and the invariants applying to the unit resolved
root -> folder -> file:

```bash
uv run minds specs export
```

Check that every `@pytest.mark.witnesses` marker under the given paths
(default: `apps/minds`) names a coordinate a corpus unit actually claims:

```bash
uv run minds specs check-witnesses
```

## Fanning out witness tests with TMR

`minds specs plan --for-tmr` emits one TMR task packet per spec unit (scenarios
and scenario outlines by default; `--include-rules` adds invariant Rules) as
JSONL. Each packet carries the unit's coordinate as its id and the full
enriched export record as its context. Feed the file to the generic task-file
recipe (`mngr tmr-tasks`, from the `mngr-tmr` plugin) with the minds
spec-witnessing prompt variants:

```bash
uv run minds specs plan --for-tmr > /tmp/spec-tasks.jsonl
uv run mngr tmr-tasks --tasks-file /tmp/spec-tasks.jsonl --name tmr-minds-specs \
  --mapper-prompt apps/minds/tmr/specs_mapper.j2 \
  --reducer-prompt apps/minds/tmr/specs_reducer.j2
```

(The root justfile wraps this as `just tmr-minds-specs`.) Each mapper agent
writes the cheapest sufficient test witnessing its unit -- specs are read-only,
tests-only by default, and every touched test gets the `witnesses` marker. The
reducer integrates the mapper branches and gates the tree: nothing under
`apps/minds/specs/` may change, `minds specs validate` and
`minds specs check-witnesses` must pass, duplicate generated tests are
deduplicated, and the blast-radius pytest subset must pass.

## Linking tests to specs

A test that verifies a spec unit declares it with the
`witnesses(coordinate, partial=...)` pytest marker; see
[testing-overview.md](./testing-overview.md).
