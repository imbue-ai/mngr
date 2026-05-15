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

In normal use, you launch the desktop client via Electron --
`just devminds-start` from this repo root for development iteration, or
the packaged Electron app for end users. Both spawn the `minds run`
backend behind the scenes; the only knob you usually need is the env
var:

```bash
export MINDS_CLIENT_CONFIG_PATH=~/.devminds/envs/josh-1.toml
just devminds-start
```

`minds run` honors `MINDS_CLIENT_CONFIG_PATH` as the default for its
`--config-file` flag, so setting the env var in your shell (or
`~/.bashrc`) makes Electron use the right tier without any further
plumbing. If you want to bypass Electron and exercise the backend
directly, you can also pass the flag explicitly:

```bash
uv run minds run --config-file <path-to-client.toml>
```

If neither `MINDS_CLIENT_CONFIG_PATH` nor `--config-file` is set, the
default is resolved in this order:

1. `apps/minds/imbue/minds/config/envs/_bundled/client.toml`, if present.
   Production Electron builds write this file via the build script when
   `MINDS_BUILD_TIER=production` is set in the build env.
2. `apps/minds/imbue/minds/config/envs/dev/client.toml`, which ships
   with the wheel.

A dynamic dev env's local override file (`~/.<root>/envs/<dev-name>.toml`)
is a full self-contained client config -- point at it via
`MINDS_CLIENT_CONFIG_PATH` or `--config-file`. No layering happens at
runtime.

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
2. Populate the Vault paths under `secrets/minds/<tier>/...` -- see
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
6. Smoke-test the backend directly:
   ```bash
   uv run minds run --config-file apps/minds/imbue/minds/config/envs/<tier>/client.toml
   ```
   Or exercise the full desktop-app path:
   ```bash
   export MINDS_CLIENT_CONFIG_PATH=apps/minds/imbue/minds/config/envs/<tier>/client.toml
   just devminds-start
   ```

## Dynamic dev environments

Each developer can stand up their own dev env on top of the shared
dev tier. Resources created per dev env: a Modal *environment* inside
the dev Modal workspace, a Neon database under the dev Neon project,
and a SuperTokens app under the dev SuperTokens core. Cloudflare,
Vultr, Anthropic, and OAuth clients are shared dev-tier resources.

```bash
# Provision (or upgrade) a dev env named `josh-1`. Idempotent: re-runs
# safely against an existing env to upgrade per-env Modal secrets and
# redeploy the Modal apps in-place.
uv run minds env deploy josh-1

# Run the full desktop app against it (Electron + backend):
export MINDS_CLIENT_CONFIG_PATH=~/.devminds/envs/josh-1.toml
just devminds-start

# Or smoke-test the backend directly without Electron:
uv run minds run --config-file ~/.devminds/envs/josh-1.toml

# See what dev envs are configured on this machine.
uv run minds env list

# Tear it down (cloud resources + local TOML).
uv run minds env destroy josh-1
```

`minds env deploy` writes its result to `~/.<root>/envs/<name>.toml`
with mode `0600`. The file holds the per-dev-env URLs (connector URL,
litellm URL) plus a `[secrets]` subtable with the new Neon DSN +
SuperTokens app id. Nothing is written back to Vault.

If a provisioning step fails partway through, `minds env deploy` rolls
back the resources it created on this run and exits non-zero. The local
TOML is only written after every provider step succeeds, so a failed
deploy never leaves a half-built local file behind. On re-run, deploy
picks up where the last successful step left off.

## Cutover from the joshalbrecht production deployment

The pre-existing `joshalbrecht`-owned `production` deployment (Modal
apps, Neon DB rows, Cloudflare tunnels, SuperTokens users) is left
running untouched. Stand up the new production tier under its dedicated
accounts, repoint
`apps/minds/imbue/minds/config/envs/production/client.toml` at the new
infra, and rebuild the Electron app with `MINDS_BUILD_TIER=production`
so new users hit the new endpoints. No migration of existing users.
