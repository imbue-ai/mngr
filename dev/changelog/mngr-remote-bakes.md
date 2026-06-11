Added canonical justfile recipes for pool-host operations: `just
bake-pool-host <attributes-json> <region> [workspace_dir] [count] [extra
flags]`, `just list-pool-hosts`, and `just destroy-pool-host <id>`, plus a
private `_pool-dsn-args` helper. These wrap the env-aware `minds pool
{create,list,destroy}` so OVH creds, the management SSH key, Vault addressing,
and the staging/production host_pool DSN are all resolved automatically -- no
hand-exported secrets.

Removed the broken `cleanup-pool-hosts` recipe: it sourced the long-gone
`.minds/<env>/neon.sh` shell files (secrets are in Vault now) and was redundant
with the connector's hourly release-cleanup cron. The new `destroy-pool-host`
recipe is the env/Vault-aware single-host replacement.

Fixed `just test-acceptance`: its marker expression was `-m "no release"`, a
pytest syntax error (`no` is not an operator) that failed at collection; it is
now `-m "not release"`.

Removed a duplicated forever-claude-template worktree-existence check block in
`just minds-start`.

Added a `minds-justfile` skill that routes any minds task (app, pool hosts,
environments, deployments, tests) through the root justfile, and directs adding
a recipe when one is missing.
