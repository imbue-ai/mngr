# Host Pool Setup

How to set up the infrastructure for LEASED mode (pre-provisioned Vultr host pool).

## Prerequisites

- Neon PostgreSQL database (two connection strings: pooled for runtime, direct for migrations)
- Vultr API key (for provisioning VPS instances)
- Modal account (for deploying the remote_service_connector)

## Step 1: Create the database table

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
    version TEXT NOT NULL,
    leased_to_user TEXT,
    leased_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);
```

Run via:
```bash
psql "$NEON_DB_DIRECT" -c "<SQL above>"
```

## Step 2: Generate the management SSH keypair

This keypair is used by the remote_service_connector to SSH into pool hosts and inject user public keys when a host is leased.

```bash
mkdir -p .minds/production/pool_management_key
ssh-keygen -t ed25519 -f .minds/production/pool_management_key/id_ed25519 -N ""
```

The private key goes into the `pool-ssh-production` Modal secret. The public key is passed to `create_pool_hosts.py` when provisioning hosts.

## Step 3: Create the Modal secrets

Two new secrets are needed (in addition to the existing `cloudflare-production` and `supertokens-production`):

### neon-production

Contains the **pooled** Neon connection string:

```bash
# .minds/production/neon.sh
export DATABASE_URL=postgresql://user:pass@host-pooler.neon.tech/db?sslmode=require
```

### pool-ssh-production

Contains the management private key:

```bash
# .minds/production/pool-ssh.sh
export POOL_SSH_PRIVATE_KEY="-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----"
```

Push both secrets to Modal:

```bash
uv run scripts/push_modal_secrets.py production
```

## Step 4: Redeploy the remote_service_connector

```bash
MNGR_DEPLOY_ENV=production modal deploy apps/remote_service_connector/imbue/remote_service_connector/app.py
```

## Step 5: Create pool hosts

Provision one or more Vultr VPS instances, stop their agents, install the management SSH key, and register them in the database:

```bash
uv run python apps/remote_service_connector/scripts/create_pool_hosts.py \
    --count 1 \
    --version <version-tag> \
    --management-public-key-file .minds/production/pool_management_key/id_ed25519.pub \
    --database-url "$NEON_DB_DIRECT"
```

The `--version` must match what the minds app will request when leasing:
- In production: the latest semver tag from the template repo (e.g., `v1.2.3`)
- During development: the branch name (e.g., `mngr/minds-onboarding`)

## Step 6: Verify

Check the pool has available hosts:

```bash
psql "$NEON_DB_DIRECT" -c "SELECT id, vps_ip, status, version FROM pool_hosts"
```

## Cleanup

Released hosts (after a user destroys a workspace) can be cleaned up with:

```bash
uv run python apps/remote_service_connector/scripts/cleanup_released_hosts.py \
    --database-url "$NEON_DB_DIRECT"
```

This destroys the Vultr VPS and removes the database row.

## Development workflow

During development, set `MINDS_WORKSPACE_BRANCH` to your branch name. The minds app uses the branch name as the version string, so pool hosts must be provisioned with the same version:

```bash
uv run python apps/remote_service_connector/scripts/create_pool_hosts.py \
    --count 1 \
    --version mngr/minds-onboarding \
    --management-public-key-file .minds/production/pool_management_key/id_ed25519.pub \
    --database-url "$NEON_DB_DIRECT"
```
