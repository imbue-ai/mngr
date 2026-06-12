# Host Pool Setup

How to set up the infrastructure for the imbue-cloud-leased pool host flow.

## Prerequisites

- Neon PostgreSQL database (two connection strings: pooled for runtime, direct for migrations)
- OVH API credentials (AK / AS / CK) for the endpoint the pool uses
  (default `ovh-us`). Pool hosts are provisioned via `mngr imbue_cloud
  admin pool create` against the OVH backend. (The older Vultr-backed
  path still works for one-off baking but every new pool flow uses
  OVH; see the `--region` option on `admin pool create`.)
- Modal account (for deploying the remote_service_connector)

## Step 1: Create the database schema

**For dev envs:** skip this step. `minds env deploy` (against a dev
env) provisions a brand-new Neon project per env and applies the
schema automatically by replaying
`apps/remote_service_connector/migrations/*.sql` against the new
`host_pool` database.

**For staging / production:** apply the schema once, by hand, against
the tier's pre-provisioned `host_pool` database. Use the **direct**
(non-pooled) Neon connection string:

```bash
for f in apps/remote_service_connector/migrations/*.sql; do
    psql "$NEON_DB_DIRECT" -f "$f"
done
```

The migrations are idempotent and apply cleanly to a fresh DB or one
that already has earlier migrations applied. The `000_initial_schema.sql`
file is the canonical full schema; `001`-`003` are defensive ALTERs
that no-op when 000 already laid the table down in its final shape.

The `attributes` JSONB column carries whatever shape the operator wants
to match leases against (`repo_branch_or_tag`, `cpus`, `memory_gb`,
`gpu_count`, etc.); the connector's `/hosts/lease` endpoint matches
`attributes @> request_attributes`.

## Step 2: Generate the management SSH keypair

Used by the remote_service_connector to inject lease-time user public keys into pool hosts:

```bash
mkdir -p .minds/production/pool_management_key
ssh-keygen -t ed25519 -f .minds/production/pool_management_key/id_ed25519 -N ""
```

The private key goes into the `pool-ssh-production` Modal secret. The public key path is passed to the bake command in step 5.

## Step 3: Populate the tier's Vault entries

Secrets live in HCP Vault now (not `.minds/<env>/` shell files); see
`apps/minds/docs/vault-setup.md` for prerequisites. For the host-pool
flow specifically:

### secrets/minds/production/neon

The **pooled** Neon connection string:

```bash
vault kv put -mount=secrets kv/minds/production/neon \
    DATABASE_URL=postgresql://user:pass@host-pooler.neon.tech/db?sslmode=require
```

### secrets/minds/production/pool-ssh

The management private key:

```bash
vault kv put -mount=secrets kv/minds/production/pool-ssh \
    POOL_SSH_PRIVATE_KEY=@.minds/production/pool_management_key/id_ed25519
```

(`@<path>` tells `vault kv put` to read the value from the file -- the
file itself never leaves the operator's laptop.)

### secrets/minds/<tier>/ovh

The shared per-tier OVH AK/AS/CK trio. Read by `minds env deploy /
destroy` (to enumerate + delete OVH VPSes belonging to a dev env) and
by `mngr imbue_cloud admin pool create` (to provision OVH-backed pool
hosts). NOT pushed to Modal.

Generate the trio once per tier at
<https://api.us.ovhcloud.com/createApp> (endpoint `ovh-us`; pick
whichever endpoint matches the pool's `--region`). Use a copy of
`.minds/template/ovh.sh` to capture the three values, then push to
Vault:

```bash
cp .minds/template/ovh.sh /tmp/production-ovh.sh
$EDITOR /tmp/production-ovh.sh
uv run scripts/push_vault_from_file.py production ovh /tmp/production-ovh.sh
shred -u /tmp/production-ovh.sh
```

The same steps work verbatim for `staging` and `dev` (substitute the
tier in the path). Dev-tier OVH credentials are shared across all
per-developer dev envs.

## Step 4: Push the Vault changes to Modal and redeploy

```bash
eval "$(uv run minds env activate --deploy production)"
uv run minds env deploy --yes-i-mean-production
```

`minds env deploy` pushes every tier secret from Vault into Modal
Secrets (`<service>-production` for every service named in
`apps/minds/imbue/minds/config/envs/production/deploy.toml`) and then
``modal deploy``s both the connector and the LiteLLM proxy against
the workspace named in the same `deploy.toml`. The
`--yes-i-mean-production` flag is the mandatory safety bar for tier
deploys; substitute `--yes-i-mean-staging` (and `activate staging`)
for the staging tier.

## Step 5: Bake one or more pool hosts

Provision a Vultr VPS, run the FCT template's `mngr create --template main --template vultr` to bake the agent state, install the management SSH key on both the VPS and the container, then write a `pool_hosts` row.

Fetch the values you need from Vault into the local shell once:

```bash
export VULTR_API_KEY=$(vault kv get -mount=secrets -field=VULTR_API_KEY kv/minds/production/pool-ssh)
export DATABASE_URL=$(vault kv get -mount=secrets -field=DATABASE_URL kv/minds/production/neon)
export ANTHROPIC_API_KEY=$(vault kv get -mount=secrets -field=ANTHROPIC_API_KEY kv/minds/production/litellm)

uv run mngr imbue_cloud admin pool create \
    --count 1 \
    --attributes '{"repo_branch_or_tag": "<branch-or-tag>"}' \
    --workspace-dir ~/project/forever-claude-template \
    --management-public-key-file .minds/production/pool_management_key/id_ed25519.pub \
    --database-url "$DATABASE_URL"
```

The `--attributes` JSON describes what the row will match against. The minds desktop client always sends `repo_branch_or_tag` in its lease request (the resolved FCT branch in dev, or the latest semver tag in production), so that key needs to be present on every row that should ever be leased. Other dimensions (`cpus`, `memory_gb`, `gpu_count`) can be set if you want a more constrained pool generation; they're only required on the row when the lease request also includes them.

### Fast path vs. slow path

When a user creates an imbue_cloud workspace, minds makes up to two `mngr create` calls:

1. **Fast path** (`fast_mode=require`): lease a pool host whose `attributes` exactly match (including `repo_branch_or_tag`) and adopt its pre-baked agent. This is fast because the host is fully baked.
2. **Slow path** (`fast_mode=prevent`): if no exact match exists, the provider raises `FastPathUnavailableError`; minds automatically retries, this time leasing *any* available host (resource attributes only -- `repo_branch_or_tag` is dropped), destroying its baked container, and rebuilding it from the FCT `Dockerfile`. This is slower (a full container build) but works whenever the pool has any free host of the right size.

So a pool whose rows are baked at an older `repo_branch_or_tag` no longer hard-fails newer workspace creations -- they fall back to the slow path. Keeping the pool baked at the current version is still worthwhile because it keeps creations on the fast path. Only when the pool is genuinely empty (no `available` rows) does creation fail, with `ImbueCloudLeaseUnavailableError`.

To rsync the local mngr working tree into the FCT worktree's `vendor/mngr/` for the duration of the bake (dev-loop pattern), pass `--mngr-source <monorepo-root>`. The bake resets `vendor/mngr/` to HEAD when it finishes, so the worktree stays clean wrt mngr churn.

List the rows:

```bash
uv run mngr imbue_cloud admin pool list --database-url "$DATABASE_URL"
```

## Step 6: Verify

```bash
psql "$NEON_DB_DIRECT" -c "SELECT id, vps_address, status, attributes FROM pool_hosts ORDER BY created_at DESC"
```

## Cleanup

Released hosts (after a user destroys their lease) can be cleaned up with:

```bash
uv run python apps/remote_service_connector/scripts/cleanup_released_hosts.py \
    --database-url "$NEON_DB_DIRECT"
```

This destroys the underlying Vultr VPS and removes the database row.

## Development workflow

During development, set `MINDS_WORKSPACE_BRANCH` to your branch name. The minds app uses that branch as the lease request's `repo_branch_or_tag`, so the pool host's `attributes.repo_branch_or_tag` must match:

```bash
uv run mngr imbue_cloud admin pool create \
    --count 1 \
    --attributes "{\"repo_branch_or_tag\": \"$(git rev-parse --abbrev-ref HEAD)\"}" \
    --workspace-dir "$PWD/.external_worktrees/forever-claude-template" \
    --management-public-key-file .minds/production/pool_management_key/id_ed25519.pub \
    --database-url "$DATABASE_URL" \
    --mngr-source "$PWD"
```
