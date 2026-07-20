# mngr-tmr

Test map-reduce plugin for [mngr](https://github.com/imbue-ai/mngr).

Collects tests via pytest, launches one agent per test to run and optionally fix failures, polls for completion, and generates an HTML report. Successful fixes are pulled into local branches and optionally merged by an integrator agent.

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
(which the `.github/workflows/tmr.yml` workflow inputs mirror). The minds variant
ships a minds-tailored mapper prompt at `apps/minds/tmr/mapper.j2`.

## Task-file runs (`mngr tmr-tasks`)

`mngr tmr-tasks` is a sibling command that fans out an explicit JSONL task file
instead of pytest collection. Each line is a task packet with `schema_version`,
`id`, optional `display_id` (used for agent/branch slugs), `kind`, and a
free-form `context` object. The file is validated up front (line-numbered
errors, duplicate ids, unsupported schema versions), one agent is launched per
task, and mapper/reducer branches come back as bundles exactly as in `mngr tmr`.

There are no packaged prompt defaults: the task semantics live with the
producer of the task file, so `--mapper-prompt` and `--reducer-prompt` are
required. The mapper template renders with `task_id`, `kind`, `context_json`
(the packet's context as pretty-printed JSON), `outcome_filename`, and
`publish_snippet`; the reducer template renders with the same context as the
packaged reducer. Mapper agents must write the same
`testing_agent_outcome.json` contract as `mngr tmr` mappers so the shared HTML
report can read it.

The canonical producer is the minds behavioral-spec corpus:
`minds specs plan --for-tmr` emits one packet per spec unit, and
`apps/minds/tmr/specs_mapper.j2` / `specs_reducer.j2` anchor agents on writing
witness tests for their unit (see `apps/minds/docs/behavioral-specs.md`). The
root `justfile` recipe `tmr-minds-specs` wraps the pair.
