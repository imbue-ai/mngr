# Environments

Minds runs against three isolated environment tiers:

- **production** -- end-user-facing, never touched by dev iteration.
- **staging** -- shape-identical to production, used for pre-prod
  validation.
- **dev** -- shared base for developer iteration. Each developer creates
  their own dynamic dev env on top of the shared dev base.

Each tier has its own Modal account, Neon account, Cloudflare account,
SuperTokens account, OAuth clients, Vultr account, Anthropic key, and
pool-management SSH keypair. There is zero cross-tier reach.

## How tier selection works

The desktop client picks its env via:

```bash
minds run --config-file <path-to-client.toml>
```

If `--config-file` is not passed, the default is resolved in this order:

1. `apps/minds/imbue/minds/config/envs/_bundled/client.toml`, if present.
   Production Electron builds write this file via the build script when
   `MINDS_BUILD_TIER=production` is set in the build env.
2. `apps/minds/imbue/minds/config/envs/dev/client.toml`, which ships
   with the wheel. This is what `uv run minds run` sees by default.

A dynamic dev env's local override file (`~/.<root>/envs/<dev-name>.toml`)
is a full self-contained client config -- you point at it with the same
`--config-file` flag, no layering happens at runtime.

## Per-tier config files

Each tier has two TOML files in
`apps/minds/imbue/minds/config/envs/<tier>/`:

- `client.toml` -- read by the desktop client. Tiny: just the URLs the
  client talks to (`connector_url`, `litellm_proxy_url`).
- `deploy.toml` -- read by deploy scripts and `minds env create`. Names
  the Modal workspace, the Vault path prefix, the Cloudflare domain, the
  OAuth client ids, and the list of services this tier pushes from Vault
  to Modal.

Secret values are **not** in either of these files. Secrets live in
HCP Vault -- see `apps/minds/docs/vault-setup.md`.

## Deploying a tier from scratch

1. Stand up the per-tier accounts (Modal workspace, Neon project,
   Cloudflare account+zone, SuperTokens core, Google+GitHub OAuth apps,
   Vultr account).
2. Populate the Vault paths under `secrets/kv/minds/<tier>/...` -- see
   the schema files at `.minds/template/*.sh`.
3. Update `apps/minds/imbue/minds/config/envs/<tier>/client.toml` and
   `deploy.toml` with the new account URLs / workspace names.
4. Push secrets to Modal:
   ```bash
   uv run scripts/push_modal_secrets.py <tier>
   ```
5. Deploy the Modal apps:
   ```bash
   scripts/deploy_remote_service_connector.sh <tier>
   scripts/deploy_litellm.sh <tier>
   ```
6. Smoke-test:
   ```bash
   uv run minds run --config-file apps/minds/imbue/minds/config/envs/<tier>/client.toml
   ```

## Dynamic dev environments

Each developer can stand up their own dev env on top of the shared
dev tier. Resources created per dev env: a Modal *environment* inside
the dev Modal workspace, a Neon database under the dev Neon project,
and a SuperTokens app under the dev SuperTokens core. Cloudflare,
Vultr, Anthropic, and OAuth clients are shared dev-tier resources.

```bash
# Provision a new dev env named `josh`.
uv run minds env create josh

# Run minds against it.
uv run minds run --config-file ~/.minds/envs/josh.toml

# See what dev envs are configured on this machine.
uv run minds env list

# Tear it down.
uv run minds env destroy josh
```

`minds env create` writes its result to `~/.<root>/envs/<name>.toml`
with mode `0600`. The file holds the per-dev-env URLs (connector URL,
litellm URL) plus a `[secrets]` subtable with the new Neon DSN +
SuperTokens app id. Nothing is written back to Vault.

If a provisioning step fails partway through, `minds env create` rolls
back the resources it already created and exits non-zero. The local
TOML is only written after every provider step succeeds, so a failed
create never leaves a half-built local file behind.

## Cutover from the joshalbrecht production deployment

The pre-existing `joshalbrecht`-owned `production` deployment (Modal
apps, Neon DB rows, Cloudflare tunnels, SuperTokens users) is left
running untouched. Stand up the new production tier under its dedicated
accounts, repoint
`apps/minds/imbue/minds/config/envs/production/client.toml` at the new
infra, and rebuild the Electron app with `MINDS_BUILD_TIER=production`
so new users hit the new endpoints. No migration of existing users.
