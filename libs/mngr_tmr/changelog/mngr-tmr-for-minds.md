Generalized TMR to run distinct test suites (e.g. the mngr suite and the minds suite) as separate, independently reviewable runs.

- Added `--name <slug>` to give a run its own variant prefix on agent, branch, and host names (e.g. `tmr-mngr/<run>/*` vs `tmr-minds/<run>/*`), so two suites' branches and PRs stay separate. It is distinct from `--run-name`, which identifies a single run within a variant. The name is validated as a slug (alphanumerics, dashes, underscores) since it becomes a branch/agent/host name segment.

- Added `--mapper-prompt` / `--reducer-prompt` to point a variant at its own Jinja prompt templates. An override template may `{% extends %}` or `{% include %}` the packaged `mapper.j2` / `reducer.j2` by name to reuse the shared body.

- The reintegrate hint in the HTML report now carries `--name` for non-default variants so the suggested command resolves the same run.
