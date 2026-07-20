# mngr-specs

Behavioral-spec corpus tooling for [mngr](https://github.com/imbue-ai/mngr).

Registers the `mngr specs` command group over a behavioral-spec corpus: a tree of
Gherkin `.feature` files (Scenarios, Scenario Outlines, and `Rule` invariants)
plus prose sidecars. It offers three subcommands:

- `validate` - parse every spec file and enforce the corpus language rules.
- `list` - emit the corpus as JSONL, one record per authored unit, with
  structural filters (`--area`, `--tag`, `--unit`, `--name`, `--step`).
- `matrix` - join the corpus against the `witnesses` markers in a test tree,
  reporting per-unit coverage.

## The corpus model

The tool is corpus-generic: a corpus is any `<project>/specs/` directory, and
every invocation names exactly one corpus via `--root`. Each independent sub-app
or plugin owns its corpus at its own root, so a codebase can spin out of the
monorepo with its corpus, its live-corpus guard test, and its `witnesses` markers
traveling together. `matrix` pairs a corpus with a test tree: `--tests` defaults
to the corpus root's parent directory (a corpus at `<project>/specs/` is
witnessed by `<project>`'s tests), and every `witnesses` coordinate must resolve
within the paired corpus.

In this repository the first corpus lives at `apps/minds/specs/`, so the
documented invocation is:

```sh
uv run mngr specs validate --root apps/minds/specs
```

## The spec language

The `.feature` language this tool serves - folders, tags, coordinates,
invariants as `Rule` blocks with folder scoping, overview/sidecar prose files,
and the `witnesses` test back-link convention - is defined by the
behavioral-specs skill (`.claude/skills/behavioral-specs/`).
