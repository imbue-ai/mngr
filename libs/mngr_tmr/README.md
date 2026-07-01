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
