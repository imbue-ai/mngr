# vault-environments

## Refined prompt

> we want to move minds to a place where we actually have multiple environments for production and dev, and where we use vault for storing the secrets (rather than just having them in local .env files and folders)
>
> Go gather all of the context for the minds app (per instructions in CLAUDE.md). Also take a look at ~/project/forever-claude-template (the default repo that we deploy)
>
> Once you've gathered that context, let me know what questions you have about this task.
>
> Note that we will *still* be pushing secrets to modal (when deploying to modal), and there's no real way around that.
> It's just that, for deployment, we want the secrets to be getting stored in (and fetched from) vault, rather than just my local files.
>
> We want to start with just two environments (production and dev), but obviously we'll end up making more in the future.
>
> Ideally we want *everything* to be isolated between the different environments, so be sure to figure out all of the things that we'll need to deploy and configure.
>
> In practice, we'll want to entirely move away from our currently-deployed services for both dev and production (the current stuff is just weirdly tied to my own accounts).
>
> Actually, while we're at it, let's make 3:  dev, staging, and production. Production and staging should be totally isolated, identical, etc, but dev should be a little more dynamic and sloppy, like, we can make dynamic environments using some shared keys (shared across developers)
> Otherwise it's going to be kind of annoying to iterate on this stuff.
>
> * Three environments to start: `dev`, `staging`, `production`. Staging and production are fully isolated and shape-identical; `dev` supports dynamic per-developer / per-branch environments sharing dev-tier base credentials.
> * **Vault scope is server-side deploy secrets only.** Vault stores secrets the Modal-deployed services need at runtime. The deploy script pulls from Vault, pushes to Modal Secrets (which persist as today), and never writes Vault values to a persistent file on the deployer's laptop. **User-side desktop-app secrets stay as shell env vars** (`ANTHROPIC_API_KEY`, `GH_TOKEN`) — unchanged.
> * Vault: HCP Vault, namespace `admin`, KV mount `secrets/`, path layout `secrets/minds/<tier>/<service>` (one Vault secret per existing `.minds/template/<service>.sh` file). Dynamic dev env secrets are not stored in Vault.
> * Vault auth: developer / CI is responsible for being logged in to the `vault` CLI themselves.
> * Modal: three separate Modal accounts (one per env tier). Within the `dev` account, dynamic deployments isolate via separate Modal **environments** (`modal deploy --env=<dev-name>`).
> * Per-service isolation: Neon (separate accounts per tier; separate DB per dev env), Cloudflare (separate accounts per tier; shared across dev envs), SuperTokens (separate accounts per tier; separate app per dev env), Google/GitHub OAuth (per-tier client; loopback callback works for every dev env), Vultr + Anthropic LiteLLM source key shared across dev envs (Vultr instances tagged `minds_dev_env=<name>`), pool-management SSH key per tier.
> * Required-secrets schema: `.minds/template/*.sh` files remain as the canonical schema of what each tier's Vault paths must contain. Static `.minds/<env>/` per-env directories go away.
> * Per-dev-env local override: single file `~/.minds/envs/<dev-name>.toml`, `chmod 600`, with secrets in a `[secrets]` subtable. Full self-contained snapshot — no runtime layering on top of the dev tier defaults.
> * Per-tier config layout: two files per tier — `<env>/client.toml` (tiny: connector URL, LiteLLM proxy URL — what the desktop client reads) + `<env>/deploy.toml` (Modal workspace, Vault paths, Cloudflare domain, OAuth client IDs — what the deploy script consumes). Devs override `client.toml` in their local file.
> * Env selection: `minds run --config-file <path>` always wins. The build process writes a specific bundled file at a known path; at runtime, if that bundled file exists it is the default, otherwise the runtime falls back to the dev-tier file. Release Electron build writes the production file there; local dev gets the dev fallback for free.
> * Dynamic dev env lifecycle: `minds env {create,list,destroy} <name>` lives in `apps/minds/imbue/minds/cli/`. `create` programmatically provisions Modal environment, Neon DB, SuperTokens app, and tags Vultr instances; on partial failure, best-effort cleanup of whatever was already created. `destroy` tears down everything `create` made *plus* any running workspace agents bound to that env. Cloudflare zone + OAuth clients are shared dev-tier resources. All resulting values get written into a self-contained `~/.minds/envs/<dev-name>.toml`.
> * Cutover: stand up new infra for all three tiers, point new builds at it, leave the existing `joshalbrecht`-owned production running untouched as a reference / debugging target. No migration.

## Overview

- Today, every environment-scoped secret lives on Josh's laptop as `.minds/<env>/<service>.sh` files and gets pushed into Modal Secrets at deploy time; every URL the desktop client uses points at a single `joshalbrecht`-owned Modal workspace. This blocks anyone else from running a real deploy and conflates "dev iteration" with "production users".
- Move the source of truth for deploy-time secrets into **HCP Vault** (namespace `admin`, mount `secrets/`, layout `secrets/minds/<tier>/<service>`). Each `.minds/template/<service>.sh` continues to be the *schema* of what a tier's Vault entry must contain.
- Stand up **three completely separate accounts** (Modal, Neon, Cloudflare, SuperTokens, plus OAuth clients) for `dev`, `staging`, and `production`. Staging and production are shape-identical; dev hosts dynamic per-developer environments.
- Within the `dev` Modal account, each dynamic dev env is its own Modal **environment** (`modal deploy --env=<dev-name>`). Each dev env also gets its own Neon DB and SuperTokens app; everything else (Cloudflare zone, OAuth clients, Vultr API key, Anthropic source key, pool-management SSH key) is shared across dev envs.
- The desktop client picks the environment it talks to via `minds run --config-file <path>`. Per-tier config is split into two files: `client.toml` (tiny — the URLs the desktop reads) and `deploy.toml` (Modal workspace, Vault paths, etc. — what the deploy pipeline consumes). User-side runtime secrets (`ANTHROPIC_API_KEY`, `GH_TOKEN`) stay as shell env vars — Vault is *not* involved client-side.
- A new `minds env {create,list,destroy}` CLI provisions and tears down dynamic dev envs programmatically; per-dev-env state lives only in `~/.minds/envs/<dev-name>.toml` on the developer's machine — never in Vault.
- No backwards compatibility. The existing `joshalbrecht`-owned production deployment is left running untouched as a reference target; new infra replaces it everywhere.

## Expected Behavior

### Deploying a tier (staging / production)

- `scripts/deploy_remote_service_connector.sh <tier>` and `scripts/deploy_litellm.sh <tier>` continue to be the entry points; their behavior changes:
  - They require `vault` CLI to be logged in to the HCP `admin` namespace (or they exit non-zero with a clear message).
  - For each `<service>` declared in `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml`'s `[secrets]` section, they read `secrets/minds/<tier>/<service>` from Vault, then push it into Modal as `<service>-<tier>` via the existing `modal secret create --force` path.
  - Vault values are held only in the deploy script's process memory; nothing is persisted to disk. Modal Secrets themselves persist (warm `min_containers=1` stays valid).
  - `MNGR_DEPLOY_ENV=<tier>` continues to drive both Modal app names and `Secret.from_name(...)` lookups inside the deployed apps.
- The `<tier>` arg must be `dev`, `staging`, or `production`. Any other value is rejected.
- After deploy, all infra for that tier (Modal apps, Modal Secrets, Neon DB rows, Cloudflare zone state, SuperTokens core, OAuth clients) is owned by accounts dedicated to that tier — there is zero cross-tier reach.

### Running the desktop client

- `minds run` accepts a new `--config-file <path>` flag. The flag determines every environment-derived URL the client uses (connector URL, LiteLLM proxy URL).
- Resolution order for the default when `--config-file` is not passed:
  1. If a known **bundled config path** (e.g. `apps/minds/imbue/minds/config/envs/_bundled/client.toml`) exists, use it. The Electron production build writes the production tier's `client.toml` there during packaging.
  2. Otherwise fall back to the dev tier's `client.toml` shipped as Python package data (`apps/minds/imbue/minds/config/envs/dev/client.toml`). This is what `uv run minds run` and any non-production build see.
- A developer working against a dynamic dev env runs `minds run --config-file ~/.minds/envs/<dev-name>.toml`. The dev override file is fully self-contained — no layering with the dev tier defaults at runtime.
- The user's own runtime secrets (`ANTHROPIC_API_KEY`, `GH_TOKEN`, `TELEGRAM_BOT_TOKEN`, etc.) continue to flow into spawned workspaces via shell env vars and the `pass-host-env` / `pass-env` declarations in the FCT template. The new config file does **not** carry these.

### Creating a dynamic dev environment

- `minds env create <name>` (where `<name>` matches the existing `[a-z0-9_-]+` pattern used for `MINDS_ROOT_NAME`):
  1. Validates `vault` CLI is logged in and the `dev` tier's `deploy.toml` resolves.
  2. Reads dev-tier shared secrets from `secrets/minds/dev/<service>` for the providers it needs to call (Neon API token, SuperTokens management key, Modal token-id/token-secret for the dev workspace, Vultr API key for tagging, Cloudflare API token if it ever needs to touch CF).
  3. Creates a Modal environment named `<name>` in the dev workspace via `modal environment create`.
  4. Creates a fresh Neon database named `minds-dev-<name>` via the Neon REST API; captures the pooled connection string.
  5. Creates a fresh SuperTokens app/tenant for `<name>` via the dev-tier SuperTokens management API; captures its connection URI + API key.
  6. Tags any new Vultr instances the dev env later creates with `minds_dev_env=<name>` (the tagging is set up server-side as part of how the dev-env-scoped `remote_service_connector` provisions hosts; no Vultr resources are created at env-create time).
  7. Pushes the resulting per-dev secrets into Modal in the dev workspace under the new Modal env (`modal secret create --force --env=<name> ...`), reusing the same template-driven shape as tier deploys.
  8. Deploys the `remote_service_connector` and `litellm-proxy` Modal apps into the new Modal env.
  9. Writes a full self-contained `~/.minds/envs/<name>.toml` (`chmod 600`) containing every value the desktop client and that dev env's tooling need (connector URL, LiteLLM URL, the new Neon DSN, the new SuperTokens app id, plus any secret values needed locally) — all under top-level keys and a `[secrets]` subtable.
- On *any* step failing partway through, `minds env create` performs best-effort cleanup of whatever it already created (delete Modal env, delete Neon DB, delete SuperTokens app, drop the partial `.toml` file) and exits non-zero with a per-step status report.
- Re-running `minds env create <name>` against an existing name fails (the operator is expected to `destroy` first); there is no resume-after-partial-success path.

### Listing and destroying dev environments

- `minds env list`:
  - Prints one row per file in `~/.minds/envs/*.toml`: name, connector URL, creation time, count of running workspace agents bound to that env (looked up via `mngr list --label workspace=...` against that env's hosts).
- `minds env destroy <name>`:
  1. Enumerates running workspace agents bound to that env (any agent on a host the dev env created) and runs `mngr destroy` for each.
  2. Deletes any Vultr instances tagged `minds_dev_env=<name>`.
  3. Deletes the dev env's Modal environment (which removes the Modal apps and Modal Secrets under it).
  4. Deletes the dev env's Neon DB.
  5. Deletes the dev env's SuperTokens app/tenant.
  6. Removes `~/.minds/envs/<name>.toml`.
  - `--keep-agents` skips step 1 (operator destroys agents themselves first). Without it, destroy is total.
- Cloudflare resources, OAuth clients, the Anthropic LiteLLM source key, and the Vultr API key itself are dev-tier-shared — destroy never touches them.

### Workspace agents and runtime secrets

- Inside a workspace (LOCAL Docker, LIMA VM, Vultr CLOUD, IMBUE_CLOUD pool host), the agent's view of secrets is **unchanged**:
  - For `IMBUE_CLOUD` launches, the LiteLLM mint flow in `mngr_imbue_cloud` continues to hand the agent a `ANTHROPIC_API_KEY` + `ANTHROPIC_BASE_URL` pair, except now the LiteLLM proxy URL it talks to is the one in the loaded `client.toml`.
  - For non-imbue-cloud modes, the desktop client's own process env (sourced from the user's shell) still flows through to the workspace via the FCT template's `pass-host-env` declarations.
- The dynamic dev env's `client.toml` provides the connector / LiteLLM URLs that scope all of the above to that dev env.

### Cutover

- The existing `joshalbrecht--remote-service-connector-production-fastapi-app.modal.run` and `joshalbrecht--litellm-proxy-production-...` apps, the Neon DB they read from, and the SuperTokens users they hold are left running untouched.
- New `production` and `staging` builds point at the new dedicated infra. No data migration; no DNS swap.

## Implementation Plan

### Vault layout (set up out-of-band, documented in repo)

- Cluster: `vault-cluster-public-vault-df29b16f.9b573ab7.z1.hashicorp.cloud:8200`, namespace `admin`, KV v2 mount `secrets/`.
- For each tier (`dev`, `staging`, `production`) and each service file in `.minds/template/*.sh` (`cloudflare`, `litellm`, `litellm-connector`, `neon`, `pool-ssh`, `supertokens`), one Vault secret at `secrets/minds/<tier>/<service>` whose key set matches the keys declared in the corresponding template file.
- A new doc at `apps/minds/docs/vault-setup.md` enumerates the paths and links the template files as the schema.

### `apps/minds/imbue/minds/config/envs/` — per-tier config

New directory tree, replacing the role of `.minds/<env>/` for non-secret config:

```
apps/minds/imbue/minds/config/envs/
  dev/
    client.toml       # connector_url, litellm_proxy_url
    deploy.toml       # modal_workspace, modal_env (default), vault_path_prefix,
                      #   cloudflare_domain, oauth_google_client_id, oauth_github_client_id,
                      #   [secrets] list naming each .minds/template/<svc>.sh file
  staging/
    client.toml
    deploy.toml
  production/
    client.toml
    deploy.toml
  _bundled/           # gitignored, populated by build
    .gitignore        # `*` (only .gitkeep/.gitignore committed)
```

- `apps/minds/imbue/minds/config/data_types.py` (existing) gains new types:
  - `ClientEnvConfig(FrozenModel)` — fields: `connector_url: AnyUrl`, `litellm_proxy_url: AnyUrl`.
  - `DeployEnvConfig(FrozenModel)` — fields: `modal_workspace: NonEmptyStr`, `modal_env: NonEmptyStr | None` (only set for dev's dynamic case), `vault_path_prefix: NonEmptyStr` (e.g. `secrets/minds/production`), `cloudflare_domain: NonEmptyStr`, `oauth_google_client_id: NonEmptyStr | None`, `oauth_github_client_id: NonEmptyStr | None`, `required_secrets: tuple[ServiceName, ...]`.
  - `LocalDevEnvConfig(ClientEnvConfig)` — extends ClientEnvConfig with `[secrets]` subtable typed as `Mapping[NonEmptyStr, SecretStr]` for any per-dev secrets the desktop side needs.
- `apps/minds/imbue/minds/config/loader.py` (new) — pure loader:
  - `load_client_config(path: Path) -> ClientEnvConfig`
  - `resolve_default_client_config_path() -> Path` returning `_bundled/client.toml` if it exists, else `dev/client.toml`.
  - `load_deploy_config(tier: str) -> DeployEnvConfig` reads `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml`.

### `apps/minds/imbue/minds/desktop_client/minds_config.py` — strip env-var URL knob

- Delete `DEFAULT_REMOTE_SERVICE_CONNECTOR_URL`, the `REMOTE_SERVICE_CONNECTOR_URL` env var, and `remote_service_connector_url` property.
- The desktop client no longer reads any tier-bound URL from `~/.minds/config.toml`. `MindsConfig` retains only genuinely user-personal settings (`default_account_id`, `auto_open_requests_panel`).
- `apps/minds/imbue/minds/cli/run.py` gains `--config-file <path>` (default resolved via `resolve_default_client_config_path()`), loads it into a `ClientEnvConfig`, and threads the loaded URLs through to the `AgentCreator` / `ImbueCloudCli` / `MindsApiUrlWriter` / etc. — wherever the connector or LiteLLM URL is needed today.
- `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/primitives.py` loses `_DEFAULT_CONNECTOR_URL` and `get_default_connector_url()`; every caller now requires an explicit URL passed in.

### `scripts/push_modal_secrets.py` — Vault source, no local files

- Replace the `.minds/<env>/` lookup with Vault reads.
- New signature: `uv run scripts/push_modal_secrets.py <tier> [--dry-run]`.
  - `<tier>` ∈ {`dev`, `staging`, `production`}. (Per-dev-env pushes do not go through this script — they go through `minds env create`.)
  - For each `<service>` listed in `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml`'s `required_secrets`:
    - Read `secrets/minds/<tier>/<service>` via `vault kv get -format=json -mount=secrets kv/minds/<tier>/<service>`.
    - Validate every key declared in `.minds/template/<service>.sh` is present (uses the existing `_parse_env_file` against the template to get the expected key set).
    - Invoke `uv run modal secret create --force <service>-<tier> KEY=VAL ...` exactly as today.
- Vault values are kept in process memory and `dict[str, str]` only. No `.sh` files are written.
- The `--dir` flag and `_TEMPLATE_DIR_NAME` plumbing are deleted. `.minds/template/*.sh` is read directly from the repo root to derive the expected key set per service.

### `scripts/deploy_remote_service_connector.sh` and `scripts/deploy_litellm.sh`

- Each becomes a thin orchestration: `uv run scripts/push_modal_secrets.py <tier>` first, then `MNGR_DEPLOY_ENV=<tier> uv run modal deploy --workspace=<workspace> --env=main apps/...`.
- The script reads `modal_workspace` from `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml` and passes it explicitly to `modal deploy`, eliminating the implicit "use whoever's logged in" surprise.

### `apps/remote_service_connector/imbue/remote_service_connector/app.py`

- The `_DEPLOY_ENV` env var continues to drive Modal app name and `Secret.from_name` lookups (`-<env>` suffix).
- Hardcoded `_MODAL_WORKSPACE = "joshalbrecht"` is removed; the fallback `_DEFAULT_CONNECTOR_DOMAIN` derivation goes away. `AUTH_WEBSITE_DOMAIN` becomes **required** in the `supertokens-<env>` Modal secret (the deploy script enforces it).
- No other changes — the app already reads everything it needs from env vars.

### `apps/modal_litellm/app.py`

- No changes beyond what `MNGR_DEPLOY_ENV` already does. The proxy URL the client uses to talk to LiteLLM is recorded in each tier's `client.toml`.

### `apps/minds/imbue/minds/cli/env.py` (new)

- New click group `env` registered on the `cli` in `apps/minds/imbue/minds/cli_entry.py` (alongside `run` and `pool`):
  - `minds env create <name>`
  - `minds env list`
  - `minds env destroy <name> [--keep-agents]`
- Subcommands delegate to functions in `apps/minds/imbue/minds/envs/provisioning.py` (new module — see below).

### `apps/minds/imbue/minds/envs/` (new package)

```
apps/minds/imbue/minds/envs/
  __init__.py
  primitives.py        # DevEnvName(NonEmptyStr), DevEnvNotFoundError, ...
  paths.py             # dev_envs_dir() -> Path, dev_env_file(name) -> Path
  local_store.py       # read/write/delete ~/.minds/envs/<name>.toml, chmod 600
  vault_reader.py      # thin wrapper around `vault kv get -format=json`
  provisioning.py      # create_dev_env(...), destroy_dev_env(...), list_dev_envs()
  providers/
    __init__.py
    modal_env.py       # create_modal_env(workspace, name), delete_modal_env(...)
    neon_db.py         # create_neon_db(api_token, project, name), delete_neon_db(...)
    supertokens_app.py # create_supertokens_app(...), delete_supertokens_app(...)
    vultr_tags.py      # list_instances_tagged(api_key, tag), delete_instances(...)
```

- `provisioning.create_dev_env(name, vault: VaultReader, dev_deploy_cfg: DeployEnvConfig, paths: DevEnvPaths)`:
  - Returns a `CreatedDevEnv` record; on partial failure rolls back every provider step that already succeeded, then re-raises wrapped in `DevEnvProvisioningError`.
- Each provider module exposes `create_*` and `delete_*` typed entry points; HTTP calls go through `httpx` with explicit `raise_for_status()`.
- `provisioning.destroy_dev_env(name, *, keep_agents: bool)`:
  - Looks up the per-dev `~/.minds/envs/<name>.toml`; if absent, raises `DevEnvNotFoundError`.
  - When `keep_agents=False`, calls `mngr destroy` for every agent whose host metadata matches the dev env (host id naming convention + label match).
  - Walks the same provider modules in reverse to delete each piece, then removes the local toml.

### Build embedding for the desktop client

- `apps/minds/electron/` build script gains a step that, when `MINDS_BUILD_TIER=production` (or `=staging`) is set in the build env, copies `apps/minds/imbue/minds/config/envs/<tier>/client.toml` into `apps/minds/imbue/minds/config/envs/_bundled/client.toml` before the Python wheel is packaged.
- When `MINDS_BUILD_TIER` is unset (e.g. `uv run minds run`, dev test runs, CI smoke), nothing is written and `resolve_default_client_config_path()` falls back to `dev/client.toml`.
- `apps/minds/imbue/minds/config/envs/_bundled/` is gitignored (committed `.gitignore` with `*`) so build artifacts never land in source control.

### Docs

- `apps/minds/docs/environments.md` (new) — the operator's guide: how to provision a tier from scratch (accounts, Vault entries, OAuth apps, deploy script invocation), how to use `minds env create/list/destroy`, how the `--config-file` resolution works.
- `apps/minds/docs/host-pool-setup.md` (existing) — rewrite step 3 (Modal secret push) and step 6 (post-deploy verification) around Vault.
- `README.md` (`scripts/push_modal_secrets.py` callouts) — update the example invocations.

### Deletions

- `.minds/<env>/*` (the per-env disk directories) are gone. `.minds/template/*.sh` stays.
- `_DEFAULT_CONNECTOR_URL` / `get_default_connector_url()` in `libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/primitives.py`.
- `DEFAULT_REMOTE_SERVICE_CONNECTOR_URL`, `_REMOTE_SERVICE_CONNECTOR_URL_ENV`, `remote_service_connector_url` from `apps/minds/imbue/minds/desktop_client/minds_config.py`.
- The associated tests in `minds_config_test.py` for the URL knob.

## Implementation Phases

Each phase produces a working system; no phase leaves the tree broken.

### Phase 1 — Config plumbing (no behavior change yet)

- Add `apps/minds/imbue/minds/config/envs/{dev,staging,production}/{client.toml,deploy.toml}` populated with the same URLs / values currently hardcoded (so the existing `joshalbrecht`-owned production stays the production target for now).
- Add `apps/minds/imbue/minds/config/loader.py` + types in `data_types.py`.
- Add `--config-file` to `minds run`. The flag defaults via `resolve_default_client_config_path()` and the desktop client now sources every connector / LiteLLM URL from the loaded `ClientEnvConfig`.
- Delete `DEFAULT_REMOTE_SERVICE_CONNECTOR_URL` and the `REMOTE_SERVICE_CONNECTOR_URL` env var.
- Delete `get_default_connector_url()` in `mngr_imbue_cloud/primitives.py`; require callers to pass a URL explicitly.

### Phase 2 — Vault as the deploy source

- Add `apps/minds/imbue/minds/envs/vault_reader.py`.
- Rewrite `scripts/push_modal_secrets.py` to read from Vault. The signature accepts a tier name; behavior is otherwise unchanged from the operator's view.
- Wire `scripts/deploy_remote_service_connector.sh` and `scripts/deploy_litellm.sh` to invoke the new flow.
- Delete the `.minds/<env>/` disk directories. `.minds/template/*.sh` stays (still the schema).
- After this phase: any tier can be deployed end-to-end from a clean machine once `vault login` is done.

### Phase 3 — Stand up dedicated tier infra

- Operator (out-of-band): create dedicated Modal / Neon / Cloudflare / SuperTokens accounts for `dev`, `staging`, `production`; create Google + GitHub OAuth clients per tier; populate `secrets/minds/<tier>/<service>` in Vault for each tier.
- Update `apps/minds/imbue/minds/config/envs/{dev,staging,production}/{client.toml,deploy.toml}` to point at the new accounts' values.
- Deploy `remote_service_connector` and `litellm-proxy` to each tier.
- Verify smoke: `minds run --config-file apps/minds/imbue/minds/config/envs/staging/client.toml` can sign in via OAuth and create an agent.

### Phase 4 — Build-time bundling

- Add the `_bundled/` directory + `.gitignore`.
- Update the Electron build pipeline to honor `MINDS_BUILD_TIER` and copy the right `client.toml` into `_bundled/`.
- Smoke: a `MINDS_BUILD_TIER=production` build of the Electron app, run without any flags, talks to new-production.

### Phase 5 — Dynamic dev envs

- Add `apps/minds/imbue/minds/envs/` package + `apps/minds/imbue/minds/cli/env.py`.
- Implement provider modules (Modal env, Neon, SuperTokens, Vultr tag query) and `provisioning.create_dev_env` / `destroy_dev_env` / `list_dev_envs`.
- Register the `env` command group on the `minds` CLI.
- Smoke: a developer runs `minds env create josh-test`, `minds run --config-file ~/.minds/envs/josh-test.toml`, exercises a workspace, then `minds env destroy josh-test`.

### Phase 6 — Cleanup

- Move the `joshalbrecht`-tied things in `mngr_imbue_cloud` / fixtures to point at the staging tier where helpful for ongoing tests.
- Remove the `--keep-agents`-less code paths if they prove unnecessary in practice. (Probably not needed; mention here so it isn't forgotten.)
- Re-read every site that references "production" — they should all now mean "new dedicated production", not "joshalbrecht-owned production". The latter remains running but unreferenced.

## Testing Strategy

### Unit tests (`_test.py`)

- `apps/minds/imbue/minds/config/loader_test.py` — round-trip TOML load for `ClientEnvConfig` and `DeployEnvConfig`; invalid URL raises a typed error; missing required key raises a typed error; the bundled-vs-dev fallback picks the right path under `tmp_path`.
- `apps/minds/imbue/minds/envs/local_store_test.py` — write / read / delete `~/.minds/envs/<name>.toml`; assert `0o600` mode after write; missing file raises `DevEnvNotFoundError`.
- `apps/minds/imbue/minds/envs/provisioning_test.py` — drive `create_dev_env` against in-memory mock implementations of each provider interface (`mock_modal_env_test.py`, `mock_neon_db_test.py`, `mock_supertokens_app_test.py`); assert the `~/.minds/envs/<name>.toml` written has the expected shape; simulate a failure at the SuperTokens step and assert `Modal env` + `Neon DB` rollback was invoked.
- `scripts/push_modal_secrets_test.py` (rename of the current test if any) — drive a `_run_push(...)` helper against a mock vault-reader, assert it invokes the right `modal secret create --force ...` argv per service and that template-schema validation rejects a Vault entry missing a declared key.
- `apps/minds/imbue/minds/desktop_client/minds_config_test.py` — delete the URL-knob tests; ensure remaining `MindsConfig` tests still pass.

### Integration tests (`test_*.py`, no marker)

- `apps/minds/test_cli_env.py` — invoke `minds env create` / `list` / `destroy` end-to-end against a fake provider set wired through a test conftest fixture; assert exit codes, stdout JSON shape (`--format json`), and side-effect ordering.
- `apps/minds/test_deploy_secrets_smoke.py` — drive `push_modal_secrets.py --dry-run staging` against a mock vault reader and assert the printed Modal command set matches what's expected.

### Acceptance / release tests (`@pytest.mark.acceptance` / `release`)

- `apps/minds/test_desktop_client_e2e.py` (existing) — extend to run with `--config-file` pointing at a temp config that points at a stub connector URL served by a local FastAPI test fixture. Confirm the desktop client never falls back to a hardcoded production URL.
- A new release-marked test exercises the full `minds env create / minds run / minds env destroy` cycle against a *real* dev tier (gated on the `vault` CLI being logged in; skipped otherwise). Marked `release` because it touches external infra.

### Manual verification

- After Phase 3: do an actual `minds run --config-file .../staging/client.toml` from a clean checkout on a machine that does *not* have Josh's credentials, sign in via OAuth, create a workspace, send a message, destroy the workspace.
- After Phase 5: `minds env create <name>` from two different developer laptops at the same time, ensure naming collisions are detected (the Modal env, Neon DB, and SuperTokens app creation calls must all return a clear conflict error).

### Ratchets / type checks

- No new TYPE_CHECKING guards.
- The `httpx` calls inside `providers/` are typed against the providers' actual response schemas (pydantic `TypeAdapter` parse, not `dict[str, Any]`).
- `test_ratchets.py` for `apps/minds` ensures the deleted `.minds/<env>/` directories don't sneak back via a regression.

## Open Questions

- **Build pipeline location for `MINDS_BUILD_TIER`**: this spec assumes the Electron build script reads the env var and copies the right `client.toml` into `_bundled/`. The actual build configuration (`apps/minds/todesktop.json`, `package.json`, CI workflow) hasn't been touched; needs a short follow-up pass to verify the hook lands where the build expects it.
- **CI authentication to Vault**: developers and operators run `vault login` interactively; CI doing real deploys needs a non-interactive method (OIDC, AppRole, or a long-lived token in a GitHub Action secret). Out of scope for this spec but blocks "CI deploys staging automatically".
- **Per-service rollback granularity in `minds env create`**: best-effort cleanup is specified, but some providers (Neon DB drops, SuperTokens app deletion) have eventual-consistency windows. The implementation needs to decide whether `create` blocks on confirmed deletion during rollback or fires the deletes and exits.
- **Cloudflare resource lifecycle in dev**: dev envs share the dev-tier Cloudflare zone. The connector's existing `/tunnels/*` flow creates tunnels per agent; nothing in this spec addresses *cleanup* of orphaned tunnels for destroyed dev envs. Likely needs a follow-up `minds env destroy` step that walks the connector's tunnel list filtered by the dev env's user id.
- **Pool host attribution across dev envs**: leased pool hosts carry `attributes.repo_branch_or_tag`. Today the dev workflow co-mingles all developers' lease requests against one pool. With dynamic dev envs each using its own connector (in its own Modal env), the dev-tier pool DB is shared — should pool rows also carry a `minds_dev_env` attribute? Otherwise destroy can't reclaim "my" pool hosts cleanly.
- **`minds env list` agent counting** assumes we can map a workspace agent back to its dev env. This works if the per-dev connector URL is captured at agent-create time and stored on the agent's host labels; need to confirm the existing labels carry enough info, or add one.
- **OAuth callback caveat**: GitHub OAuth's loopback flow is more constrained than Google's (registered callback must match exactly). If GitHub sign-in is required for dev envs, the dev-tier GitHub OAuth app's callback list may need a few well-known localhost ports declared, or we accept Google-only OAuth for dev.
