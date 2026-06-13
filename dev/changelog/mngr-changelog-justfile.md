Renamed the changelog tooling scripts so they all share a `changelog_` prefix
and sort together: `consolidate_changelog.py` -> `changelog_consolidate.py`,
`trigger_changelog_consolidation.py` -> `changelog_schedule_utils.py` (the old
name implied it triggered something; it only holds the schedule's shared
identifiers + plugin-disable args), and `setup_changelog_agent.sh` ->
`changelog_deploy.sh`. All internal imports, docstrings, and the consolidation
prompt were updated to match.

Added three justfile recipes:

- `just release [args...]` wraps `scripts/release.py` (args forward as-is).

- `just changelog-deploy` wraps `scripts/changelog_deploy.sh` to (re)deploy the
nightly changelog-consolidation schedule.

- `just changelog-trigger` runs the consolidation on demand (the same agent the
schedule runs nightly), opening a PR.

`scripts/release.py`'s pre-release gate now points users at `just
changelog-trigger` to consolidate pending entries, instead of printing a long
`mngr schedule run ... --disable-plugin ...` one-liner.

`changelog_deploy.sh` now reads the agent's `GH_TOKEN` and `ANTHROPIC_API_KEY`
from Vault (`secrets/mngr/dev/github` and `secrets/mngr/dev/anthropic`) at
deploy time instead of from the operator's environment; run `vault login
-method=oidc` first. `VAULT_ADDR`/`VAULT_NAMESPACE` default to the imbue HCP
cluster.
