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

The private key goes into Vault at `secrets/minds/production/pool-ssh`
(step 3), from which `minds env deploy` pushes it to the
`pool-ssh-production` Modal secret (for the connector's lease-time SSH) and
`minds pool create` derives the matching public key at bake time. You no
longer pass the public key to the bake by hand.

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

Baking provisions an OVH VPS, runs the FCT template's `mngr create --template
main --template ovh` to build + bake the agent state, installs the management
SSH key on both the VPS and the container, then writes a `pool_hosts` row.

Use the canonical justfile recipe, after activating the tier:

```bash
eval "$(uv run minds env activate production)"   # or `staging`
just bake-pool-host '{"repo_branch_or_tag": "<branch-or-tag>"}' US-WEST-OR
```

`just bake-pool-host <attributes-json> <region> [workspace_dir] [count] [extra flags]`
wraps `minds pool create` (`apps/minds/imbue/minds/cli/pool.py`), the env-aware
layer that, from the activated tier:

- derives the management SSH public key from the tier's
  `secrets/minds/<tier>/pool-ssh.POOL_SSH_PRIVATE_KEY` Vault entry -- the same
  key the connector loads at lease time, so bake-time and lease-time SSH always
  match (you never generate or pass a key by hand);
- reads the tier's OVH AK/AS/CK from `secrets/minds/<tier>/ovh` and injects
  them into the bake;
- auto-tags the VPS `minds_env=<tier>` so `minds env destroy` can tear it down;
- for staging / production, reads the host_pool DSN from
  `secrets/minds/<tier>/neon.DATABASE_URL` (those tiers keep no local
  secrets.toml); dev / ci envs auto-resolve it from their per-env secrets.toml.

The `--attributes` JSON only *labels* the row for lease matching -- it does NOT
select the baked version. **The baked version comes entirely from the
`workspace_dir` checkout** (default `~/project/forever-claude-template`), so
check that workspace out at the branch/tag you want baked before running. The
minds desktop client always sends `repo_branch_or_tag` in its lease request
(the resolved FCT branch in dev, or the latest semver tag in production), so
that key must be present on every row that should ever be leased. Other
dimensions (`cpus`, `memory_gb`, `gpu_count`) can be set for a more constrained
pool generation; they're only required on the row when the lease request also
includes them.

Under the hood `minds pool create` shells out to `mngr imbue_cloud admin pool
create` (in `libs/mngr_imbue_cloud`), the provider-generic host-creation step
that takes a required `--region`, repeatable `--tag KEY=VALUE`, and an explicit
`--management-public-key-file`. Call it directly only for non-minds / one-off
baking outside an activated env.

### Fast path vs. slow path

When a user creates an imbue_cloud workspace, minds makes up to two `mngr create` calls:

1. **Fast path** (`fast_mode=require`): lease a pool host whose `attributes` exactly match (including `repo_branch_or_tag`) and adopt its pre-baked agent. This is fast because the host is fully baked.
2. **Slow path** (`fast_mode=prevent`): if no exact match exists, the provider raises `FastPathUnavailableError`; minds automatically retries, this time leasing *any* available host (resource attributes only -- `repo_branch_or_tag` is dropped), destroying its baked container, and rebuilding it from the FCT `Dockerfile`. This is slower (a full container build) but works whenever the pool has any free host of the right size.

So a pool whose rows are baked at an older `repo_branch_or_tag` no longer hard-fails newer workspace creations -- they fall back to the slow path. Keeping the pool baked at the current version is still worthwhile because it keeps creations on the fast path. Only when the pool is genuinely empty (no `available` rows) does creation fail, with `ImbueCloudLeaseUnavailableError`.

To rsync the local mngr working tree into the FCT worktree's `vendor/mngr/`
for the duration of the bake (dev-loop pattern), forward `--mngr-source
<monorepo-root>` as an extra flag through the recipe. The bake resets
`vendor/mngr/` to HEAD when it finishes, so the worktree stays clean wrt mngr
churn.

List the rows (with the tier activated):

```bash
just list-pool-hosts
```

## Step 6: Verify

```bash
psql "$NEON_DB_DIRECT" -c "SELECT id, vps_address, status, attributes FROM pool_hosts ORDER BY created_at DESC"
```

## Cleanup

Destroy a specific pool host (cancels its OVH VPS, then drops the row):

```bash
just list-pool-hosts                          # find the row id (tier activated)
uv run minds pool destroy <pool-host-id>      # OVH creds read from the tier's Vault entry
```

Released hosts (after a user destroys their lease) can be bulk-cleaned with:

```bash
uv run python apps/remote_service_connector/scripts/cleanup_released_hosts.py \
    --database-url "$NEON_DB_DIRECT"
```

Both paths cancel the underlying OVH VPS and remove the database row.

## Development workflow

During development, set `MINDS_WORKSPACE_BRANCH` to your branch name. The minds
app uses that branch as the lease request's `repo_branch_or_tag`, so the pool
host's `attributes.repo_branch_or_tag` must match. Bake against your dev env
(the DSN auto-resolves from its `secrets.toml`, and `--mngr-source` rsyncs your
live mngr tree into the FCT worktree's `vendor/mngr/` for the bake):

```bash
eval "$(uv run minds env activate dev-<your-user>)"
just bake-pool-host \
    "{\"repo_branch_or_tag\": \"$(git rev-parse --abbrev-ref HEAD)\"}" \
    US-WEST-OR \
    "$PWD/.external_worktrees/forever-claude-template" \
    1 \
    --mngr-source "$PWD"
```
