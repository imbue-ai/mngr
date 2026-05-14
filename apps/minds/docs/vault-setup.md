# Vault Setup

Deploy-time secrets for the minds Modal apps (`remote_service_connector`,
`litellm-proxy`) are stored in **HCP Vault** and pushed to Modal Secrets
by the deploy scripts. This doc describes the Vault layout each tier
expects, plus what every operator needs on their machine.

User-side secrets (`ANTHROPIC_API_KEY`, `GH_TOKEN`, etc.) do **not** go
through Vault; they stay as shell env vars on the operator's machine.

## Prerequisites

- The HCP Vault cluster at
  `vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200`,
  namespace `admin`, KV v2 mount `secrets/`.
- A local install of the `vault` CLI:
  <https://developer.hashicorp.com/vault/install>
- The operator is responsible for running `vault login` themselves before
  any deploy script; minds never touches the user's Vault token.

```bash
export VAULT_ADDR=https://vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200
export VAULT_NAMESPACE=admin
vault login -method=oidc   # or whatever your team is set up for
```

## Path layout

Every tier has the same set of secrets, distinguished only by the tier
name in the path:

```
secrets/kv/minds/<tier>/cloudflare
secrets/kv/minds/<tier>/litellm
secrets/kv/minds/<tier>/litellm-connector
secrets/kv/minds/<tier>/neon
secrets/kv/minds/<tier>/paid-accounts
secrets/kv/minds/<tier>/pool-ssh
secrets/kv/minds/<tier>/supertokens
```

The schema for each `<service>` is the corresponding file under
`.minds/template/<service>.sh` at the repo root. `push_modal_secrets.py`
validates every key declared by the template against the Vault entry
before pushing anything to Modal, so missing keys are caught before they
break a deploy.

`<tier>` is one of `dev`, `staging`, `production`. Dynamic dev env
secrets are **not** stored in Vault -- they live on the developer's
machine only.

## Populating a tier

For each service:

```bash
# Example: the cloudflare entry for staging.
vault kv put -mount=secrets kv/minds/staging/cloudflare \
    CLOUDFLARE_API_TOKEN=...   \
    CLOUDFLARE_ACCOUNT_ID=...  \
    CLOUDFLARE_ZONE_ID=...     \
    CLOUDFLARE_DOMAIN=staging.example.com \
    CLOUDFLARE_ALLOWED_IDPS=
```

Keys with intentionally empty values (e.g. an unused optional override)
should be set with an empty string -- the deploy script will skip those
when pushing to Modal but the template-keys-present validation still
passes.

## Deploying

Once Vault is populated:

```bash
# Push every tier secret from Vault to Modal as <service>-<tier>.
uv run scripts/push_modal_secrets.py staging

# Deploy the Modal apps.
scripts/deploy_remote_service_connector.sh staging
scripts/deploy_litellm.sh staging
```

The deploy script reads `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml`
for the Modal workspace name to pin against.

## Dynamic dev envs and Vault

`minds env create <name>` reads a small set of dev-tier secrets from
Vault (the dev-tier Neon API token, the dev-tier SuperTokens admin key,
the dev-tier Vultr API key) to provision per-dev-env resources. The
resulting per-dev-env state (Neon DSN, SuperTokens app id, etc.) is
written **only** to `~/.<root>/envs/<name>.toml` on the developer's
machine -- never back into Vault.
