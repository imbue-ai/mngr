# mngr-tmr

Test map-reduce plugin for [mngr](https://github.com/imbue-ai/mngr). It ships two
recipes over the same map -> reduce machinery, differing in their scope anchor:

- `mngr tmr` (docstring-anchored): collects tests via pytest and launches one
  agent per test; each test's docstring is the contract for what it verifies.
- `mngr tmr-specs` (spec-anchored): scans a behavioral-spec corpus (see
  `mngr specs`, from `imbue-mngr-specs`) and launches one agent per `.feature`
  file; each agent creates or updates the tests witnessing that file's spec
  units, keeping the `witnesses(coordinate, partial=...)` markers honest. The
  corpus itself is read-only to the whole pipeline: mappers may only propose
  spec edits via the report's spec-escalations section, and an integrated
  branch that touches the corpus is mechanically refused.

Both poll agents to completion, pull successful work into local branches,
integrate them with a reducer agent, and generate an HTML report (the spec
recipe's report adds a per-coordinate coverage matrix of claimed vs verified
coverage).

## Variants

A single TMR command can serve distinct test suites as separate, independently
reviewable runs. A variant is just a set of CLI flags:

- `--name <slug>` sets the prefix for the run's agent, branch, and host names
  (e.g. `tmr-mngr` produces `tmr-mngr/<run>/*` branches, `tmr-minds` produces
  `tmr-minds/<run>/*`). This keeps two suites' branches, agents, and PRs
  separate. It is distinct from `--run-name`, which identifies one run within a
  variant.
- The test paths / markers after `--` select which suite runs (the mngr and
  minds suites are separated by path: `libs/...` vs `apps/minds`).
- `--mapper-prompt` / `--reducer-prompt` point a variant at its own Jinja
  prompt templates. An override template may `{% extends %}` or `{% include %}`
  the packaged `mapper.j2` / `reducer.j2` by name to reuse the shared body.
- `--env` supplies any credentials a variant needs.

Example (two variants):

```bash
mngr tmr libs/mngr  --name tmr-mngr  -- -m "release and not docker and not docker_sdk"
mngr tmr apps/minds --name tmr-minds --mapper-prompt apps/minds/tmr/mapper.j2 -- -m "release and not minds_deployment and not minds_services and not minds_snapshot_resume"
```

Variant definitions live in the caller, not in a registry inside this package.
The canonical flag sets are the root `justfile` recipes `tmr-mngr` / `tmr-minds`
/ `tmr-specs-minds` (the `.github/workflows/tmr.yml` workflow inputs mirror the
first two). The minds variants ship minds-tailored mapper prompts under
`apps/minds/tmr/`.

The spec recipe's variants work the same way, with one refinement: the packaged
`spec_mapper.j2` defines two named block slots -- `project_guidance` (where new
witnessing tests go in the target project's test taxonomy) and `infra_blockers`
(host-capability knowledge) -- so a variant template `{% extends %}` it and
fills the slots instead of forking the contract body. `apps/minds/tmr/
specs_mapper.j2` is the exemplar. Fork a self-contained copy (as the
docstring-recipe minds variant does) only when a variant must *remove* parts of
the packaged contract.
