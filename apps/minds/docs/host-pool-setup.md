# Host Pool Setup

How to set up the infrastructure for the imbue-cloud-leased pool host flow.

Pool hosts are **bare-metal slices**: lima/QEMU VMs carved on bare-metal boxes we
operate. (The boxes are currently rented from OVH, but that is an internal
implementation detail of the slice backend; other suppliers may be added later.)

> **Deprecated:** baking new **OVH classic VPS** pool hosts (one VPS per host) is
> no longer supported. The pool is baked exclusively with slices. Existing OVH VPS
> pool hosts already in the pool keep working and can still be listed and
> destroyed -- see [Legacy OVH VPS teardown](#legacy-ovh-vps-teardown).

## Prerequisites

- Neon PostgreSQL database (two connection strings: pooled for runtime, direct for migrations)
- One or more **bare-metal boxes** registered + prepped via the
  `mngr imbue_cloud admin server` commands (see
  [Step 5](#step-5-bake-one-or-more-pool-hosts)). Slice baking targets an
  explicitly-chosen `ready` box.
- Bare-metal box supplier credentials (currently OVH API AK / AS / CK). These
  order the bare-metal boxes that slices run on, and also tear down legacy OVH
  VPS hosts.
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
`minds pool create` uses it to authorize the pool key on each slice at carve
time. You no longer pass the public key to the bake by hand.

## Step 3: Populate the tier's Vault entries

Secrets live in HCP Vault now (not `.minds/<env>/` shell files); see
`apps/minds/docs/vault-setup.md` for prerequisites. For the host-pool
flow specifically:

### secrets/minds/production/neon

The **pooled** Neon connection string:

Each key is its own single-`value` leaf (see `vault-setup.md` for the split
layout):

```bash
vault kv put -mount=secrets minds/production/neon/DATABASE_URL \
    value=postgresql://user:pass@host-pooler.neon.tech/db?sslmode=require
```

### secrets/minds/production/pool-ssh

The management private key:

```bash
vault kv put -mount=secrets minds/production/pool-ssh/POOL_SSH_PRIVATE_KEY \
    value=@.minds/production/pool_management_key/id_ed25519
```

(`@<path>` tells `vault kv put` to read the value from the file -- the
file itself never leaves the operator's laptop.)

### secrets/minds/<tier>/ovh

The shared per-tier bare-metal box supplier credentials (currently OVH
AK / AS / CK). Read by `mngr imbue_cloud admin server` (to order the
bare-metal boxes that slices run on) and by `minds env deploy / destroy`
(to enumerate + delete legacy OVH VPSes belonging to a dev env). NOT
pushed to Modal.

Generate the trio once per tier at
<https://api.us.ovhcloud.com/createApp> (endpoint `ovh-us`; pick
whichever endpoint matches the boxes' region). Use a copy of
`.minds/template/ovh.sh` to capture the three values, then push to
Vault:

```bash
cp .minds/template/ovh.sh /tmp/production-ovh.sh
$EDITOR /tmp/production-ovh.sh
uv run scripts/push_vault_from_file.py production ovh /tmp/production-ovh.sh
shred -u /tmp/production-ovh.sh
```

The same steps work verbatim for `staging` and `dev` (substitute the
tier in the path). Dev-tier credentials are shared across all
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

Pool hosts are baked as bare-metal slices. A slice bake carves a lima VM on a
`ready` bare-metal box, runs the FCT template's `mngr create --template main
--template pool_host` to build + bake the agent state inside it, then writes a
`pool_hosts` row (`backend_kind=slice`).

First register + prep the bare-metal box(es) the slices will be carved on (the
box must be `ready` and have a free slot):

```bash
# Order / register / prep a bare-metal box; see `--help` on each subcommand.
uv run mngr imbue_cloud admin server order   ...   # order a box from the supplier
uv run mngr imbue_cloud admin server register ...  # record it in bare_metal_servers
uv run mngr imbue_cloud admin server setup --server-id <id>   # reinstall (injects our host key) + prep -> `ready`
uv run mngr imbue_cloud admin server list          # find the ready box's id
```

`server prep --server-id <id>` re-runs just the prep step (qemu/lima/tooling +
image staging). It SSHes the box with strict host-key pinning, so the box's sshd
host key must already be recorded on its `bare_metal_servers` row -- which
`server setup` does at OS reinstall, or `admin pool backfill-host-keys` captures
once for a box installed out of band. `prep` fails closed (no trust-on-first-use)
if no host key is recorded.

Then bake slices onto a chosen box, after activating the tier:

```bash
eval "$(uv run minds env activate production)"   # or `staging`
just bake-slice-prod US-WEST-OR v0.3.0 1 --server-id <bare-metal-server-id>
```

The `just bake-slice-{dev,prod}` recipes wrap `minds pool create --backend slice`
(`apps/minds/imbue/minds/cli/pool.py`), the env-aware layer that, from the
activated tier:

- reads the pool SSH private key from the tier's
  `secrets/minds/<tier>/pool-ssh/POOL_SSH_PRIVATE_KEY` Vault leaf -- the same
  key the connector loads at lease time, so bake-time and lease-time SSH always
  match (you never generate or pass a key by hand);
- for staging / production, reads the host_pool DSN from
  `secrets/minds/<tier>/neon/DATABASE_URL` (those tiers keep no local
  secrets.toml); dev / ci envs auto-resolve it from their per-env secrets.toml.

The `region` argument is the lease-region **label** stamped on each row (what the
connector region-matches at lease time, e.g. `US-EAST-VA`) -- not the box's raw
datacenter code.

The `--attributes` JSON only *labels* the row for lease matching -- it does NOT
select the baked version. **The baked version comes entirely from the bake
source:** `--from-tag <tag>` (production; clones the FCT remote at an exact tag)
or `--workspace-dir <dir>` (dev; a working tree, default
`~/project/forever-claude-template`). The minds desktop client always sends
`repo_branch_or_tag` in its lease request (the resolved FCT branch in dev, or the
latest semver tag in production), so that key must be present on every row that
should ever be leased. Other dimensions (`cpus`, `memory_gb`, `gpu_count`) can be
set for a more constrained pool generation; they're only required on the row when
the lease request also includes them. For slices, the per-slice size
(`memory_gb` / `cpus`) is computed from the box and stamped automatically.

Under the hood `minds pool create --backend slice` shells out to `mngr
imbue_cloud admin pool create` (in `libs/mngr_imbue_cloud`), the provider-generic
host-creation step. Call it directly only for non-minds / one-off baking outside
an activated env.

### Fast path vs. slow path

When a user creates an imbue_cloud workspace, minds makes up to two `mngr create` calls:

1. **Fast path** (`fast_mode=require`): lease a pool host whose `attributes` exactly match (including `repo_branch_or_tag`) and adopt its pre-baked agent. This is fast because the host is fully baked.
2. **Slow path** (`fast_mode=prevent`): if no exact match exists, the provider raises `FastPathUnavailableError`; minds automatically retries, this time leasing *any* available host (resource attributes only -- `repo_branch_or_tag` is dropped), destroying its baked container, and rebuilding it from the FCT `Dockerfile`. This is slower (a full container build) but works whenever the pool has any free host of the right size.

So a pool whose rows are baked at an older `repo_branch_or_tag` no longer hard-fails newer workspace creations -- they fall back to the slow path. Keeping the pool baked at the current version is still worthwhile because it keeps creations on the fast path. Only when the pool is genuinely empty (no `available` rows) does creation fail, with `ImbueCloudLeaseUnavailableError`.

To rsync the local mngr working tree into the FCT worktree's `vendor/mngr/`
for the duration of the bake (dev-loop pattern; see
`apps/minds/docs/vendor-mngr-sync.md` for the sync mechanisms), forward
`--mngr-source <monorepo-root>` as an extra flag through the recipe. The bake
resets `vendor/mngr/` to HEAD when it finishes, so the worktree stays clean wrt
mngr churn.

List the rows (with the tier activated):

```bash
just list-pool-hosts
```

## Step 6: Verify

```bash
psql "$NEON_DB_DIRECT" -c "SELECT id, vps_address, status, backend_kind, attributes FROM pool_hosts ORDER BY created_at DESC"
```

## Cleanup

Destroy a specific pool host (tears down its underlying machine, then drops the
row -- for a slice it destroys the lima VM and frees the box slot; for a legacy
OVH VPS row it cancels the VPS):

```bash
just list-pool-hosts                          # find the row id (tier activated)
just destroy-pool-host <pool-host-id>         # creds + DSN from the tier's Vault entry
```

Released hosts (after a user destroys their lease) can be bulk-cleaned with:

```bash
uv run python apps/remote_service_connector/scripts/cleanup_released_hosts.py \
    --database-url "$NEON_DB_DIRECT"
```

Both paths tear down the underlying machine (slice VM or legacy OVH VPS) and
remove the database row.

## Development workflow

During development, set `MINDS_WORKSPACE_BRANCH` to your branch name. The minds
app uses that branch as the lease request's `repo_branch_or_tag`, so the pool
host's `attributes.repo_branch_or_tag` must match. Bake against your dev env
(the DSN auto-resolves from its `secrets.toml`, and `--mngr-source` rsyncs your
live mngr tree into the FCT worktree's `vendor/mngr/` for the bake):

```bash
eval "$(uv run minds env activate dev-<your-user>)"
just bake-slice-dev \
    US-WEST-OR \
    "$PWD/.external_worktrees/forever-claude-template" \
    1 \
    --server-id <bare-metal-server-id> \
    --repo-branch-or-tag "$(git rev-parse --abbrev-ref HEAD)" \
    --mngr-source "$PWD"
```

## Legacy OVH VPS teardown

Baking new OVH classic VPS pool hosts is deprecated, but VPS hosts already in the
pool keep working until they are migrated off and destroyed. Until then:

- `just list-pool-hosts` lists every pool host, including legacy `ovh_vps` rows.
- `just destroy-pool-host <id>` tears down a legacy VPS row: it cancels the
  underlying OVH VPS and drops the row. This uses the per-tier OVH credentials
  from `secrets/minds/<tier>/ovh`.
- `minds env destroy` removes every OVH VPS belonging to a whole dev env (it
  walks the OVH IAM `minds_env=<env>` tags).
- The connector releases + an hourly Modal cron sweep any stragglers, cancelling
  the OVH VPS for released `ovh_vps` rows.

The per-tier OVH credentials therefore remain required for as long as any legacy
VPS host exists (and for ordering bare-metal slice boxes).
