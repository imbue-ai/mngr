# Environments

Minds runs against three isolated environment tiers, plus any number
of per-developer dynamic envs on top of the dev tier:

- **production** -- end-user-facing, never touched by dev iteration.
- **staging** -- shape-identical to production, used for pre-prod
  validation.
- **dev** -- shared base credentials (Modal workspace, Vault prefix,
  Cloudflare zone, etc.). Each developer creates their own dynamic dev
  env on top of the shared dev base; each dev env gets its own Modal
  environment, Neon DB, and SuperTokens app inside the shared dev
  accounts.

Each tier has its own Modal account, Neon account, Cloudflare account,
SuperTokens account, OAuth clients, bare-metal box supplier account
(currently OVH), Anthropic key, and pool-management SSH keypair. There
is zero cross-tier reach.

## Per-env data root

Every minds env owns one data root:

| Env name              | Data root                | `MINDS_ROOT_NAME`    |
|-----------------------|--------------------------|----------------------|
| `production`          | `~/.minds/`              | `minds`              |
| `staging`             | `~/.minds-staging/`      | `minds-staging`      |
| `dev-<your-user>`     | `~/.minds-dev-<your-user>/` | `minds-dev-<your-user>` |
| `dev-josh-1` (any dev) | `~/.minds-dev-josh-1/`  | `minds-dev-josh-1`   |

By convention dev env names lead with the tier (`dev-`) so the
`MINDS_ROOT_NAME` always reads tier-first: `minds-dev-<your-user>`,
`minds-dev-josh-1`, etc. The validation regex (see below) does not
enforce the prefix, but the docs and command examples assume it.

Each root holds its own mngr profile, agents, auth, logs, and (for
dev envs) the per-env `client.toml` + chmod-0600 `secrets.toml`. Two
envs activated in parallel shells never see each other's state.

`MINDS_ROOT_NAME` must match the regex `minds(-<env-name>)?` where
`<env-name>` is the same shape as `DevEnvName`
(`[a-z0-9][a-z0-9_-]{0,38}[a-z0-9]`). Legacy values (e.g. a stale
`MINDS_ROOT_NAME=devminds` from before the per-env-data-roots refactor)
are silently treated as unset and the bootstrap falls back to the
production root with a warning -- the operator can clean up via
`rm -rf ~/.devminds/` when convenient.

## Activation is the central UX

`minds env activate <name>` prints shell-sourceable exports that point
the rest of the stack at the env's data root:

```bash
eval "$(uv run minds env activate dev-<your-user>)"
```

Activation has two modes:

- **Use-only (default)**: exports the use-side env vars below and emits
  `unset MODAL_PROFILE`. This is what every non-deploying user wants --
  the desktop client, mngr, Latchkey, etc. work against the activated
  env without touching the operator's Modal CLI auth state. A
  previously-deploy-activated shell that gets re-activated in use-only
  mode flips back cleanly: the `unset MODAL_PROFILE` line drops the
  stale workspace pin before the next `modal …` shellout can pick it up.
- **Deploy mode (`--deploy`)**: additionally exports `MODAL_PROFILE`
  pinned to the tier's `modal_workspace`. Required for `minds env
  deploy` / `destroy` / `recover` (they refuse without it -- see "Deploy
  mode" below). Pre-validates that `~/.modal.toml` has a profile matching
  the tier's `modal_workspace` and refuses up front with a
  `modal token set --profile <workspace>` hint otherwise. Skipped when
  the tier's `deploy.toml` is missing or its `modal_workspace` is still
  the literal `CHANGE_ME` placeholder.

Exported use-side variables (both modes):

- `MINDS_ROOT_NAME` -- e.g. `minds-dev-<your-user>` (or just `minds`
  for production).
- `MNGR_HOST_DIR` -- e.g. `$HOME/.minds-dev-<your-user>/mngr`.
- `MNGR_PREFIX` -- e.g. `minds-dev-<your-user>-`.
- `MINDS_CLIENT_CONFIG_PATH` -- for dev envs, the per-env
  `~/.minds-<name>/client.toml` (written by `minds env deploy`).
  For `staging` / `production`, the in-repo
  `apps/minds/imbue/minds/config/envs/<tier>/client.toml` (committed
  to the repo).

Deploy-mode adds:

- `MODAL_PROFILE` -- the tier's `modal_workspace` from
  `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml`. Pins every
  subsequent `modal` CLI shellout (`modal deploy`, `modal secret create`,
  etc.) to the right Modal account regardless of which profile is marked
  `active = true` in `~/.modal.toml`. **Prerequisite:** the operator
  must have a matching profile entry in `~/.modal.toml` for each tier
  they operate against (`modal token set --profile <workspace>` once
  per tier).

To deactivate:

```bash
eval "$(uv run minds env deactivate)"
```

Source runs (`uv run minds run`, `just minds-start`, `propagate_changes`,
the `forward-system-interface` recipe, etc.) refuse to start without
activation -- no implicit default.

Behaviour by env type:

- `production` / `staging`: activation auto-creates the env root if it
  doesn't exist yet. The in-repo `client.toml` must exist (it ships
  with the repo).
- Any other name: validated as a `DevEnvName`. When the env root
  doesn't exist yet, activate refuses *unless* `--create` is passed
  (which idempotently `mkdir`s the env root and proceeds). The
  refusal message tells the operator how to bootstrap a fresh dev env
  in one line:

  ```bash
  eval "$(uv run minds env activate --create --deploy dev-<your-user>)"
  uv run minds env deploy
  ```

The packaged Electron app sets all four variables itself from the
bundled config (see "Build embedding for the desktop client" below),
so end users never `eval` anything.

## Per-tier static config

Each tier has one committed file in
`apps/minds/imbue/minds/config/envs/<tier>/`:

```
apps/minds/imbue/minds/config/envs/
  dev/
    deploy.toml       # modal_workspace, modal_env, vault_path_prefix,
                      # cloudflare_domain, [secrets] services = [...]
  staging/
    client.toml       # connector_url, litellm_proxy_url
    deploy.toml       # same shape as dev/deploy.toml
  production/
    client.toml
    deploy.toml
  _bundled/           # gitignored, populated by the Electron build
    .gitignore        # `*` (only .gitignore committed)
```

The dev tier has NO shared `client.toml` -- every dev env carries its
own URLs in `~/.minds-<env-name>/client.toml`, which `minds env deploy`
writes when it provisions the env.

Staging and production `client.toml` files are committed by hand on
the rare occasions the URLs change (typically only when standing up
the tier from scratch). Modal-driven URLs are deterministic at the
canonical short names used for these tiers, so day-to-day re-deploys
don't change them.

`client.toml` carries only **public URLs** -- never secrets. The
`ClientEnvConfig` pydantic model has `extra="forbid"` and the dev
deploy writer (`write_client_config` in `envs/local_store.py`) only
serializes the URL fields, so a stray `[secrets]` block cannot end up
in any committed staging/production file. A unit test in
`local_store_test.py` asserts this end-to-end.

Secret values (API keys, DSNs, connection URIs) live in HCP Vault --
see `apps/minds/docs/vault-setup.md`.

## Deploy mode

`minds env deploy`, `minds env destroy`, and `minds env recover` all
refuse to run unless the shell is *deploy-activated* (i.e. the operator
used `minds env activate --deploy <name>`). "Deploy-activated" is
detected via `MODAL_PROFILE`: it must be set in the environment and
must equal the tier's `modal_workspace` from `deploy.toml`. A missing or
mismatched `MODAL_PROFILE` is a hard error -- the refusal points the
operator at the exact `eval "$(uv run minds env activate --deploy
<name>)"` to re-run.

This split exists because activating `staging` (or any other tier) to
*use* the deployed services should not require Modal CLI credentials
for that tier's workspace. Bundling the two -- which prior versions did
-- caused `mngr observe` Modal discovery to fail (and Latchkey to break)
on every developer who had not added the tier's workspace to their
`~/.modal.toml`.

## Deploying a tier (staging / production)

```bash
eval "$(uv run minds env activate --deploy staging)"
uv run minds env deploy --yes-i-mean-staging
```

(For production: `activate --deploy production` + `--yes-i-mean-production`.)

The `--yes-i-mean-<tier>` flag is mandatory for tier deploys -- the
unified deploy CLI uses the same code path for dev and tier deploys,
and the flag is the explicit confirmation barrier so an accidental
`minds env deploy` (e.g. while activated against production after a
context switch) can never silently fire.

What a tier deploy does:

1. For every service listed in `deploy.toml`'s `[secrets].services`,
   reads `<vault_path_prefix>/<service>` from Vault and pushes the
   non-empty subset into Modal as `<service>-<tier>` (in the Modal
   environment named by `deploy.toml`'s `modal_env`, default `main`).
2. Runs `modal deploy` for `llm-<tier>` (after running the
   Prisma schema push) and `rsc-<tier>`.
3. Writes **nothing to disk** -- no local file changes, no edits to
   the in-repo `client.toml`. The committed `client.toml` is the
   source of truth; the operator updates it by hand on the rare
   occasions the deploy URLs change.

Tier deploys are idempotent -- re-running picks up any new Vault
values and re-deploys both apps in place.

`minds env destroy` for `staging` requires `--yes-i-mean-staging`.
Production destroy is hard-refused regardless of any flag --
production tier teardown is operator-managed outside this CLI.

Destroy is "do everything `deploy` created, plus clear out the
env-specific data that's accumulated inside operator-managed shared
resources". For staging destroy this means:

1. `mngr destroy` every agent under `~/.minds-staging/mngr/agents/`
   so containers / pool hosts / tunnels stop cleanly before their
   cloud resources go away.
2. `modal app stop` both deployed apps in the tier's Modal env.
3. `modal secret delete` every per-tier Modal Secret (`<service>-staging`).
4. Wipe SuperTokens user / session data by delete+recreate of the
   tier's app with the same `app_id` (the operator's connection URI
   + API key in Vault stay valid).
5. Wipe the Neon DB by running `DROP SCHEMA public CASCADE; CREATE
   SCHEMA public;` against `DATABASE_URL` from Vault (the DB itself
   + its DSN stay valid).
6. Enumerate and delete every Cloudflare tunnel tagged with
   `metadata.env = "staging"` (created via the connector's
   `cf_create_tunnel` -- see "Tier generation id + activate auto-wipe"
   below).
7. Delete any bare-metal resource (currently OVH) tagged
   `minds_env=staging`, including any legacy OVH VPS hosts.
8. Delete the tier generation id from Vault (so the next deploy mints
   a fresh one).
9. Only after every cloud-side step succeeds, `rmdir`
   `~/.minds-staging/`. On any partial failure the env root stays so
   the operator can re-run `destroy` to pick up where things broke
   (rather than silently leaking expensive cloud resources because
   the local pointer is gone).

Dev env destroy follows the same shape but operates on the per-dev
Modal env / Neon DB / SuperTokens app (which deploy created outright,
so destroy deletes them outright too rather than wiping data inside).
The Cloudflare-tunnel + bare-metal-resource (currently OVH) +
mngr-agent + env-root-removal steps are identical.

## Tier generation id + activate auto-wipe

`minds env deploy` for a tier mints a uuid + stores it at
`secrets/minds/<tier>/generation` in Vault, then pushes it as
`MINDS_TIER_GENERATION_ID=<uuid>` in the tier's
`litellm-connector-<tier>` Modal Secret. The connector exposes the
value at `GET /generation` (no auth required -- the id is non-sensitive).

`minds env destroy` removes the Vault entry, so the next deploy mints
a *new* uuid. Subsequent activations on any developer's machine see
the changed uuid and know their local state is stale.

`minds env activate <tier>` fetches `/generation` from the connector
URL, compares against the dev's per-env
`~/.minds-<env-name>/last_seen_generation` marker, and on mismatch
auto-wipes the env's `mngr/`, `auth/`, and `logs/` subdirs before
exporting the activation env vars. Network / parse errors during the
fetch log a warning and fall through (don't block activation).

The same flow applies to dev envs that have a per-env `client.toml`
(i.e. ones that have been deployed at least once) -- on a dev's own
machine this is rarely useful (they alone control destroy of their own
env), but the symmetry keeps the flow simple. Skipped silently when
the env root has no `client.toml` yet (fresh `activate --create`
before the first deploy).

## Running the desktop client

For source runs:

```bash
eval "$(uv run minds env activate <name>)"
uv run minds run                       # or `just minds-start`
```

`minds run` reads `MINDS_CLIENT_CONFIG_PATH` for the config to load.
A `--config-file <path>` flag overrides the env var. Refuses to start
when neither is set.

For the packaged Electron app, see "Build embedding for the desktop
client" below -- the runtime exports `MINDS_ROOT_NAME` and passes
`--config-file` automatically from the embedded bundle.

## Dynamic dev environments

Each developer can stand up their own dev env on top of the shared
dev tier. Resources created per dev env: a Modal *environment* inside
the shared dev Modal workspace, a Neon *project* (named
`minds-<env>`) under the shared dev Neon org -- with `host_pool` and
`litellm_cost` databases provisioned inside -- and a SuperTokens app
under the shared dev SuperTokens core. Cloudflare, the bare-metal box
supplier account (currently OVH), Anthropic, and OAuth clients are
dev-tier shared.

The per-env Neon project gives every dev env atomic, isolated state
for pool host rows and LiteLLM spend tracking. `minds env destroy`
deletes the project outright (everything inside goes with it); no
cross-dev contamination, no leftover roles to clean up.

Bootstrap a brand-new dev env:

```bash
# 1. Activate the env in deploy mode (--create idempotently mkdirs
#    ~/.minds-<name>/ if missing; --deploy pins MODAL_PROFILE for the
#    deploy step in step 2).
eval "$(uv run minds env activate --create --deploy dev-<your-user>)"

# 2. Deploy: provisions the Modal env, Neon project (with host_pool +
#    litellm_cost DBs), SuperTokens app, pushes per-env Modal Secrets,
#    runs `modal deploy` for both apps, and writes
#    ~/.minds-dev-<your-user>/{client.toml,secrets.toml}.
uv run minds env deploy

# 3. Re-activate in use-only mode so subsequent `mngr` / `minds run`
#    invocations don't carry a stale MODAL_PROFILE, then launch the
#    desktop client against the new env:
eval "$(uv run minds env activate dev-<your-user>)"
just minds-start
```

(For a one-off env tied to a feature you're working on, replace
`dev-<your-user>` with e.g. `dev-<your-user>-3`.)

Re-deploy in place (idempotent -- picks up any new tier-shared Vault
values and re-deploys both Modal apps):

```bash
eval "$(uv run minds env activate --deploy dev-<your-user>)"
uv run minds env deploy
```

Tear it down (cloud resources + the env root):

```bash
eval "$(uv run minds env activate --deploy dev-<your-user>)"
uv run minds env destroy
# `minds env destroy` rmdir's ~/.minds-dev-<your-user> after success;
# clear your shell with `eval "$(uv run minds env deactivate)"`.
```

See what envs exist on this machine (globs `~/.minds*/` directly):

```bash
uv run minds env list
```

## On-disk file layout per env

For a dev env named `dev-josh-3`:

```
~/.minds-dev-josh-3/
  client.toml         # connector_url, litellm_proxy_url (mode 0644)
  secrets.toml        # NEON_HOST_POOL_DSN, NEON_LITELLM_DSN,
                      #   SUPERTOKENS_CONNECTION_URI, SUPERTOKENS_API_KEY (mode 0600)
  mngr/               # this env's mngr profile (MNGR_HOST_DIR)
    agents/...
    profiles/...
  auth/               # OAuth / SuperTokens session state
  logs/
    minds.log
    minds-events.jsonl
  ...
```

`NEON_HOST_POOL_DSN` is also the DSN `mngr imbue_cloud admin pool
create` defaults to when invoked from this activated shell -- no need
to pass `--database-url` explicitly.

For production (`~/.minds/`) or staging (`~/.minds-staging/`): same
layout *minus* `client.toml` and `secrets.toml` under the env root --
those tiers source the URLs from the committed in-repo
`apps/minds/imbue/minds/config/envs/<tier>/client.toml` and the
secrets from Vault.

## Build embedding for the desktop client

The Electron build (`apps/minds/scripts/build.js`) reads two env vars
to bake a per-build configuration into `_bundled/`:

- `MINDS_CLIENT_CONFIG_BUNDLE=<path>` -- the `client.toml` to embed.
  For staging / production / beta builds, set this to the in-repo
  `apps/minds/imbue/minds/config/envs/<tier>/client.toml` or any other
  `client.toml` carrying the right URLs.
- `MINDS_ROOT_NAME_BUNDLE=<minds(-<env-name>)?>` -- the
  `MINDS_ROOT_NAME` the packaged runtime should export. A production
  build uses `minds` (writes to `~/.minds/`); a staging build uses
  `minds-staging` (writes to `~/.minds-staging/` so it never collides
  with an installed prod build); a beta build can use any name.

Both must be set together. When both are unset (the dev-mode
`pnpm start` case), `_bundled/` stays empty and the runtime relies on
the user's activated shell. Setting only one of the two fails the
build loudly.

At runtime, the Electron startup reads `_bundled/root_name` (if
present) and exports `MINDS_ROOT_NAME` + the derived `MNGR_HOST_DIR`
/ `MNGR_PREFIX` before launching `minds run`, then passes
`--config-file <bundled-path>` so the backend has no implicit
fallback to chase.

## Tier setup from scratch

Done once per tier (production / staging), out of band:

1. Stand up the per-tier accounts (Modal workspace, Neon project,
   Cloudflare account+zone, SuperTokens core, Google+GitHub OAuth apps,
   bare-metal box supplier account, currently OVH).
2. Populate the Vault paths under `secrets/minds/<tier>/...` -- see
   the schema files at `.minds/template/*.sh`.
3. Update `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml` with
   the new account URLs / workspace names. (And populate
   `<tier>/client.toml` with the URLs Modal will report once the apps
   are deployed -- typically a one-line edit per app.)
4. Deploy the Modal apps via the unified path:
   ```bash
   eval "$(uv run minds env activate --deploy <tier>)"
   uv run minds env deploy --yes-i-mean-<tier>
   ```
5. Smoke-test:
   ```bash
   just minds-start    # with the tier still activated
   ```

For dev-tier setup, the same steps but with no shared `client.toml`
(every developer creates their own dev env on top of the shared dev
base via `minds env deploy`).

## Cleaning up the legacy `~/.devminds/`

If you ever used the pre-refactor layout (one shared `~/.devminds/`
for all dev iteration plus `~/.devminds/envs/<dev-name>.toml` per-env
overrides), that root is now obsolete. There is no migration script:

```bash
rm -rf ~/.devminds/    # when convenient -- nothing in the new code reads it
```

A stale `MINDS_ROOT_NAME=devminds` left in a parent shell is harmless:
the bootstrap logs a warning, falls back to the production root, and
proceeds. Activated envs always win over inherited vars, so an
in-shell `minds env activate <name>` immediately fixes a misconfigured
parent shell.
