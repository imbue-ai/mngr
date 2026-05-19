# Per-dev-env Neon project

## Problem

Today the dev tier mixes three different sharing axes for Neon state:

| Resource | Sharing | Source |
|---|---|---|
| `host-pool` DB | tier-shared | `secrets/minds/dev/neon.DATABASE_URL` (pool admin CLI writes here) |
| `litellm-cost` DB | tier-shared | `secrets/minds/dev/litellm.DATABASE_URL` (proxy reads + writes) |
| `minds-dev-<env>` DB | per-env | created by `create_neon_database`, **overrides** `secrets/minds/dev/neon.DATABASE_URL` inside the per-env Modal Secret only |

The split is broken end-to-end:

- `mngr imbue_cloud admin pool create` writes pool host rows into `host-pool` (tier-shared).
- The deployed connector for a dev env reads its `pool_hosts` query against `minds-dev-<env>` (per-env, because the per-env `neon-dev` Modal Secret overrides the DSN).
- The two never agree -- the connector cannot see the rows the admin script just baked.

`litellm-cost` is tier-shared with no per-env override, so all dev envs would collide on virtual keys and spend tracking once any of it actually ran.

A per-env DB also drags along its own `minds_dev` role (created by `create_neon_database`) -- pointless: the role name has nothing per-env about it, and no other dev-tier resource creates per-env roles.

## Goal

One axis of sharing for Neon: **per dev env**. Each dev env owns a Neon *project* that contains both the pool DB and the litellm DB. All other dev-tier resources are already per-env (Modal env, SuperTokens app, OVH IAM tag scope, Cloudflare tunnel tags), so making Neon match removes the special-case.

Staging / production keep the existing tier-shared-vault-DSN model unchanged. They each already live in their own Neon project on their own account; no refactor needed there.

## Resource model after this change

For a dev env named `dev-josh-1`:

```
Neon project   minds-dev-josh-1                       <- new, replaces minds-dev-<env> DB
├── branch   main (default, ships with project)
│   ├── role neondb_owner                             <- default Neon role; we don't create extras
│   ├── DB   neondb                                   <- default; unused, harmless
│   ├── DB   host_pool                                <- created by deploy, schema applied
│   └── DB   litellm_cost                             <- created by deploy, LiteLLM Prisma migration applied
└── pooler   ep-<auto>-pooler.<region>.aws.neon.tech  <- one per project (Neon-managed)
```

Underscores not hyphens in DB names because the OG `host-pool` / `litellm-cost` used hyphens for no good reason and unquoted identifiers in psql / Postgres expect snake_case. We don't break anything by renaming since this is fresh state.

Tier-shared Neon entries on staging / production stay shape-identical to today (a single DB per tier). The convention is now "you get one DB per concern per env -- the env may be a tier or a dev env."

## Vault changes

### `secrets/minds/dev/neon-admin` (unchanged shape, repurposed semantics)

Today:
```
NEON_API_TOKEN=<token with scopes to create DBs in NEON_PROJECT_ID>
NEON_PROJECT_ID=raspy-lake-82340275      <- the one shared project
```

After:
```
NEON_API_TOKEN=<token with scopes to create *projects* in the dev org>
NEON_ORG_ID=org-jolly-cell-77900540      <- the org that owns per-env projects
```

The token Neon issues for an org can create + delete projects under that org. `NEON_PROJECT_ID` becomes `NEON_ORG_ID`. Operators do this once when standing up the dev tier.

### `secrets/minds/dev/neon` (unchanged shape)

Stays at `DATABASE_URL=<some DSN>`. For dev-tier deploys the value is **overridden** at deploy time to point at the per-env project's `host_pool` DB. Operators can still populate it with a tier-default DSN (used today by `mngr imbue_cloud admin pool create` invocations that bypass `minds env activate`); the per-env override wins inside the connector's Modal Secret.

### `secrets/minds/dev/litellm` (unchanged shape, semantics tightened)

Stays at `DATABASE_URL=<some DSN>` + the other LiteLLM keys. Same override story: dev-tier deploys override `DATABASE_URL` with the per-env project's `litellm_cost` DB. Staging / production keep the tier-shared value.

## On-disk state changes

`~/.minds-<env>/secrets.toml` grows two new fields (replacing the single `NEON_POOLED_DSN`):

```toml
[secrets]
NEON_HOST_POOL_DSN  = "postgresql://...host_pool..."
NEON_LITELLM_DSN    = "postgresql://...litellm_cost..."
SUPERTOKENS_CONNECTION_URI = "..."
SUPERTOKENS_API_KEY = "..."
```

The pool admin CLI reads `NEON_HOST_POOL_DSN` to default `--database-url`.

## Code surface changes

### `imbue/minds/envs/providers/neon_db.py` -- core rewrite

- Rename `NeonDatabaseRecord` -> `NeonProjectRecord`. Fields:
  - `project_id: str` (newly-created)
  - `host_pool_dsn: SecretStr`
  - `litellm_cost_dsn: SecretStr`
- Replace `create_neon_database(name, project_id, api_token)` with `create_neon_project(name, *, org_id, api_token)`:
  1. `POST /projects` with `{"project": {"name": f"minds-{name}", "org_id": org_id, "pg_version": 17, "region_id": "aws-us-west-2"}}`. Returns project + default branch.
  2. Wait for the project to be ready (poll `/projects/<id>` until `provisioner_status == "ready"`, or retry on 423 like the existing helpers).
  3. `POST /projects/<id>/branches/<branch>/databases` for `host_pool` (owner = `neondb_owner` default).
  4. Same for `litellm_cost`.
  5. `GET /projects/<id>/connection_uri?...&pooled=true` for each DB.
  6. Apply the `pool_hosts` schema to `host_pool` via psql shellout (the LiteLLM Prisma migration runs later inside `deploy_litellm_proxy`, so we don't need to touch `litellm_cost` here).
- Replace `delete_neon_database` with `delete_neon_project(name, *, org_id, api_token)`:
  - Look up project by name `minds-<name>` under `org_id` via `GET /projects`. (Storing the project id locally is fragile across destroy-then-redeploy on a new machine.)
  - `DELETE /projects/<id>`. 404 = success (idempotent).
- `wipe_neon_db_schema(dsn, ...)` stays as-is. It's only used by tier destroys (staging / production), which keep the single-DSN model.

### Pool schema bootstrap

The existing `apps/remote_service_connector/migrations/*.sql` files are the canonical schema. Apply them in sequence (001 → 002 → 003) via psql shellout. They're already idempotent.

Add a tiny helper:
```python
def apply_pool_hosts_schema(host_pool_dsn: SecretStr, *, parent_cg: ConcurrencyGroup) -> None
```
that finds the migrations dir (relative to this module via `parents`) and runs each `.sql` file in lexicographic order against the dsn.

### `imbue/minds/envs/per_env_deploy.py`

`compute_per_env_overrides`:
- Take `NeonProjectRecord` instead of `NeonDatabaseRecord`.
- Add a `litellm` override key:
  ```python
  "litellm": {"DATABASE_URL": neon_record.litellm_cost_dsn.get_secret_value()},
  "neon":    {"DATABASE_URL": neon_record.host_pool_dsn.get_secret_value()},
  ```

### `imbue/minds/envs/provisioning.py`

`ProviderCredentials`:
- Rename `neon_project_id` -> `neon_org_id` (string).

`Providers` protocol:
- `create_neon_db` -> `create_neon_project` (signature change: `org_id` instead of `project_id`).
- `delete_neon_db` -> `delete_neon_project` (same).

Per-env deploy flow (`deploy_dev_env`):
- Calls `providers.create_neon_project(name, org_id, api_token)`.
- Writes both DSNs to `~/.minds-<name>/secrets.toml` (under `NEON_HOST_POOL_DSN` / `NEON_LITELLM_DSN`).

Per-env destroy flow:
- Calls `providers.delete_neon_project(name, org_id, api_token)`.

Tier destroy path (`_wipe_neon_for_tier`): unchanged. Still reads `DATABASE_URL` from the tier vault and `DROP SCHEMA public`s it. Staging / production still own a single tier-shared DB.

### `imbue/minds/cli/env.py`

`_load_dev_credentials_from_vault`:
- Read `NEON_ORG_ID` instead of `NEON_PROJECT_ID` from `secrets/minds/dev/neon-admin`.

The CLI wrappers (`_create_neon_for_provider` / `_delete_neon_for_provider`) get renamed to `_create_neon_project_for_provider` / `_delete_neon_project_for_provider` and call the new module functions.

### `mngr_imbue_cloud/cli/admin.py` (pool admin CLI)

`pool create` / `pool list` / etc. currently take `--database-url` from `DATABASE_URL` env var or `--database-url` flag. Change the resolution order:

1. Explicit `--database-url` flag (highest precedence; ops escape hatch).
2. `MINDS_HOST_POOL_DSN` env var (could be set manually).
3. The activated minds env's `~/.minds-<env>/secrets.toml.secrets.NEON_HOST_POOL_DSN` (read transparently when `MINDS_ROOT_NAME` is activated to a dev env).
4. Refuse with a useful error if none resolved.

Concretely: a small helper `_resolve_pool_database_url(explicit: str | None) -> str` lives in admin.py and is called from every command that takes `--database-url`. Operators running pool ops outside a minds-activated shell (rare, but possible) pass `--database-url` explicitly.

### Documentation

- `apps/minds/docs/environments.md` -- update "Per-tier static config" + the "Dynamic dev envs and Vault" sections to mention `NEON_ORG_ID` and per-env Neon projects.
- `apps/minds/docs/host-pool-setup.md` -- delete the manual `CREATE TABLE pool_hosts` step; replace with "Applied automatically by `minds env deploy` against the per-env Neon project. Staging / production operators still apply it manually against the tier's Neon DB via `psql $DATABASE_URL -f apps/remote_service_connector/migrations/001_...sql`." Mention the `secrets/minds/dev/neon-admin` schema change (`NEON_ORG_ID` field).
- `apps/minds/docs/vault-setup.md` -- update the `neon-admin` row to say `NEON_API_TOKEN`, `NEON_ORG_ID` (and call out that the token must have org-scope project create permission).
- `.minds/template/neon-admin.sh` -- update the template to declare `NEON_ORG_ID` (replacing `NEON_PROJECT_ID`).

## Migration path for the existing dev project

The existing `raspy-lake-82340275` (`imbue-minds-dev`) is a per-tier project with the shared `host-pool` and `litellm-cost` DBs. Under the new model, each dev env owns its own project; the shared project has no role.

Decision: **delete the shared dev project entirely** as part of this PR. Done via the Neon API right before the code change lands. The vault entries (`neon-admin`, `neon`, `litellm`) get updated:

- `neon-admin`: drop `NEON_PROJECT_ID`, add `NEON_ORG_ID=org-jolly-cell-77900540`.
- `neon`: clear `DATABASE_URL` (no per-tier dev DB anymore; per-env deploy writes the runtime override). The vault entry still has to exist so the deploy can find it for the override layering.
- `litellm`: clear `DATABASE_URL` (same story).
- The `minds_dev` role goes away with the project.

There's no live data to migrate; we already audited `host-pool` (empty) and `litellm-cost` (2 leftover test keys you minted by hand). Both can be discarded.

## Tests

- Update `provisioning_test.py` fake providers to take the new signatures (`create_neon_project` returning `NeonProjectRecord`, etc.).
- Add a test that `compute_per_env_overrides` returns both `neon.DATABASE_URL` and `litellm.DATABASE_URL` overrides.
- Add a test that `deploy_dev_env` writes both DSNs to `secrets.toml`.
- Existing tests for `wipe_neon_db_schema` (tier destroy path) keep working since that helper is unchanged.

## Out of scope for this PR

- Pool admin commands acting "implicitly on the activated env's pool" for staging / production. Today the tier-shared model still works fine for those. Dev-env activation gets the new resolution; tier activation falls through to `DATABASE_URL` env var / explicit flag.
- A schema for managing Neon org membership / API tokens (out-of-band operator setup).
- Changing the LiteLLM Prisma migration mechanism (still runs on first `deploy_litellm_proxy` call against the per-env `litellm_cost` DB; takes ~14 min on first deploy per env).
