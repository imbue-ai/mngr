The TMR workflow no longer opens the run's pull request itself. The reducer agent does, so the description can carry the run's actual findings (mapper status breakdown, escalations table) instead of just a link to the report.

The workflow now passes the reducer -- and only the reducer -- `GH_TOKEN` plus the context it needs to write that description (repository, base branch, run URL, and the periodic-run label/assignees), via the new `--reducer-env` option. Mappers do not receive the token.

The only PR-related step left in the workflow is the breadcrumb comment linking a superseded periodic PR to its replacement, which reads the new PR's URL from a `pull_request_url` event the orchestrator emits.
