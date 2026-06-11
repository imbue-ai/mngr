Added canonical justfile recipes for pool-host operations: `just
bake-pool-host <attributes-json> <region> [workspace_dir] [count] [extra
flags]` and `just list-pool-hosts`, plus a private `_pool-dsn-args` helper.
These wrap the env-aware `minds pool create` / `minds pool list` so OVH creds,
the management SSH key, Vault addressing, and the staging/production host_pool
DSN are all resolved automatically -- no hand-exported secrets.

Added a `minds-justfile` skill that routes any minds task (app, pool hosts,
environments, deployments, tests) through the root justfile, and directs adding
a recipe when one is missing.
