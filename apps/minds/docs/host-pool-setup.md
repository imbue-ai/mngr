# Host Pool Setup

How to set up the infrastructure for the imbue-cloud-leased pool host flow.

## Prerequisites

- Neon PostgreSQL database (two connection strings: pooled for runtime, direct for migrations)
- Vultr API key (for provisioning VPS instances)
- Modal account (for deploying the remote_service_connector)

## Step 1: Create the database schema

Use the **direct** (non-pooled) Neon connection string for schema migrations:

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE pool_hosts (
    id UUID PRIMARY KEY,
    vps_ip TEXT NOT NULL,
    vps_instance_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    ssh_port INTEGER NOT NULL,
    ssh_user TEXT NOT NULL,
    container_ssh_port INTEGER NOT NULL,
    status TEXT NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    leased_to_user TEXT,
    leased_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX pool_hosts_attributes_gin ON pool_hosts USING GIN (attributes);
```

Run via:
```bash
psql "$NEON_DB_DIRECT" -c "<SQL above>"
```

The `attributes` JSONB column carries whatever shape the operator wants to match leases against (`repo_branch_or_tag`, `cpus`, `memory_gb`, `gpu_count`, etc.); the connector's `/hosts/lease` endpoint matches `attributes @> request_attributes`.

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

Push every tier secret from Vault to Modal:

```bash
uv run scripts/push_modal_secrets.py production
```

## Step 4: Redeploy the remote_service_connector

```bash
scripts/deploy_remote_service_connector.sh production
```

This re-runs the Vault->Modal push and then ``modal deploy``s the app
against the workspace named in
`apps/minds/imbue/minds/config/envs/production/deploy.toml`.

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

To rsync the local mngr working tree into the FCT worktree's `vendor/mngr/` for the duration of the bake (dev-loop pattern), pass `--mngr-source <monorepo-root>`. The bake resets `vendor/mngr/` to HEAD when it finishes, so the worktree stays clean wrt mngr churn.

List the rows:

```bash
uv run mngr imbue_cloud admin pool list --database-url "$DATABASE_URL"
```

## Step 6: Verify

```bash
psql "$NEON_DB_DIRECT" -c "SELECT id, vps_ip, status, attributes FROM pool_hosts ORDER BY created_at DESC"
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
