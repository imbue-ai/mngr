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

Each tier has two families of Vault entries:

**Pushed to Modal at deploy time** (the connector + litellm-proxy read
these from their runtime env via `modal.Secret.from_name(...)`):

```
secrets/minds/<tier>/cloudflare
secrets/minds/<tier>/litellm
secrets/minds/<tier>/litellm-connector
secrets/minds/<tier>/neon
secrets/minds/<tier>/paid-accounts
secrets/minds/<tier>/pool-ssh
secrets/minds/<tier>/supertokens
```

**Read only by `minds env deploy` on a developer's laptop** (never
pushed to Modal -- the connector's runtime doesn't need
create-database / VPS-management permissions):

```
secrets/minds/<tier>/neon-admin   # NEON_API_TOKEN, NEON_PROJECT_ID
secrets/minds/<tier>/vultr        # VULTR_API_KEY
```

The schema for each `<service>` is the corresponding file under
`.minds/template/<service>.sh` at the repo root. `minds env deploy`
validates every key declared by a Modal-pushed template against the
Vault entry before pushing anything to Modal, so missing keys are
caught before they break a deploy.

`<tier>` is one of `dev`, `staging`, `production`. Per-dev-env secrets
(the values `minds env deploy` generates per developer for a dev env)
are **not** stored in Vault -- they live on the developer's machine
only in `~/.minds-<name>/secrets.toml` (mode 0600).

## Populating a tier

For each service, copy the template, fill in the values, and push:

```bash
cp .minds/template/litellm.sh /tmp/dev-litellm.sh
$EDITOR /tmp/dev-litellm.sh
uv run scripts/push_vault_from_file.py dev litellm /tmp/dev-litellm.sh
shred -u /tmp/dev-litellm.sh
```

The helper validates that every key declared by the template is present
in the filled file (empty values are fine -- the deploy step skips them
when pushing to Modal), pushes the entry, and prints a `shred` command
for cleanup.

## Deploying

All deploys (dev / staging / production) flow through the unified
`minds env deploy` CLI on the activated env:

```bash
# Tier deploys (staging / production):
eval "$(uv run minds env activate staging)"
uv run minds env deploy --yes-i-mean-staging

# Dev env deploys (per-developer):
eval "$(uv run minds env activate <your-user>-dev)"
uv run minds env deploy
```

`minds env deploy` reads `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml`
for the Modal workspace name + the list of services to push from
Vault, then runs `modal deploy` for both `litellm-proxy-<tier>` and
`remote-service-connector-<tier>`. Tier deploys write nothing to disk
(the committed in-repo `client.toml` stays the source of truth); dev
env deploys write the resulting URLs to `~/.minds-<name>/client.toml`
and per-env secrets (Neon DSN, SuperTokens connection URI + API key)
to `~/.minds-<name>/secrets.toml` (mode 0600).

The `--yes-i-mean-<tier>` flag is a mandatory safety bar for tier
deploys. `minds env destroy` is dev-env-only and hard-refuses for
`production` / `staging` -- tier teardown is operator-managed outside
this CLI.

## Dynamic dev envs and Vault

`minds env deploy` (when run with a dev env activated) reads a small
set of dev-tier secrets from Vault (the dev-tier Neon API token, the
dev-tier SuperTokens admin key, the dev-tier Vultr API key) to
provision per-dev-env resources. The resulting per-dev-env state
(Neon DSN, SuperTokens app id, etc.) is written **only** to
`~/.minds-<name>/secrets.toml` on the developer's machine -- never
back into Vault. Staging / production never write a local
`secrets.toml`; the same values for those tiers live in Vault and are
pushed straight to Modal on each deploy.
