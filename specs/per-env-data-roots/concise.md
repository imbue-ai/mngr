# per-env-data-roots

## Refined prompt

> we want to refactor the way that data is stored for dev/staging environments
>
> Right now, information is stored in ~/.devminds/*
>
> This is a little silly now that we can dynamically create development environments.
>
> Instead, the information should end up in ~/.minds-<env-name>/* for all environments *except* production (which should clearly go to just ~/.minds/*)
>
> Note that this involves setting both MNGR_HOST_DIR and MNGR_PREFIX properly (seen minds env activate), as well as some other vars (defined in there)
>
> * The existing `~/.devminds/` data is treated as obsolete — no migration script, just docs that tell users to `rm -rf ~/.devminds/` when convenient.
> * Env-name validation keeps the existing `[a-z0-9][a-z0-9_-]{0,38}[a-z0-9]` regex; the `<user>-<suffix>` shape is documented as convention only.
> * Per-env on-disk state is split into two files in `~/.minds-<env-name>/`: a non-secret `client.toml` (connector URL, litellm proxy URL, etc.) and a separate chmod-600 `secrets.toml` (Neon DSN, SuperTokens connection URI + API key, etc. — values that for staging/production would otherwise come from / be pushed to Vault); only dev envs write a local `secrets.toml`.
> * For staging/production, the non-secret `client.toml` lives in the repo at `apps/minds/imbue/minds/config/envs/<tier>/client.toml`, is committed by hand, and is updated rarely (most fields are static — Modal-driven URLs are deterministic at the canonical short names used for these tiers).
> * `minds env deploy` for `staging`/`production` does not write any deploy artifact to disk (no generated values to capture); it only pushes Vault secrets to Modal and runs `modal deploy`. For dev envs it writes both `client.toml` and the chmod-600 `secrets.toml` under `~/.minds-<env-name>/`.
> * `MINDS_CLIENT_CONFIG_PATH` defaults to the non-secret `client.toml` when `MINDS_ROOT_NAME` is set.
> * `MINDS_ROOT_NAME` remains the single knob; activation sets it to `minds-<env-name>` (e.g. `minds-josh-3`) and `MNGR_PREFIX` to `minds-<env-name>-`, with validation enforcing the `minds(-<name>)?` shape.
> * Legacy `MINDS_ROOT_NAME` values (e.g. `devminds`) that don't match `minds(-<name>)?` are treated as unset — silently fall back to production / `~/.minds/` with a warning logged.
> * Outer-shell mutation stays on the `eval "$(minds env activate <name>)"` pattern — no shell-function init layer.
>
> We also have some skills, justfile entries, and other places that will need to get smarter about selecting which environment to use.
>
> * `devminds-*` justfile recipes are replaced by generic env-agnostic ones that require the env to be activated in the shell first and fail loudly with "run `minds env activate <name>` first" when unset (no `MINDS_ROOT_NAME=devminds` defaulting). Every script/recipe that touches mngr state (propagate_changes, forward-* recipes, etc.) requires an activated env — no defaulting to prod.
>
> The idea is that, in general, an environment should be *activated* in a shell before running such commands.
>
> Environments can therefor be easily listed by looking at which ~/.minds-* folders exist.
>
> Environments should, be convention, begin with the name of the user and then a "-" and then the name or number of the environment (ex: josh-3)
> Most devs will probably just go with <name>-dev for their standard single dev env that they have and are working with (when they're not specifically working on features that require an env)
>
> * Production stays at `~/.minds/` and staging gets its own permanent `~/.minds-staging/` so `minds env activate staging` works the same way as any dev env.
> * `minds env list` globs `~/.minds-*/` directly and shows every env root on disk; production (`~/.minds/`) appears as a special row.
> * For staging/production, `minds env activate` sets `MINDS_CLIENT_CONFIG_PATH` to the in-repo `apps/minds/imbue/minds/config/envs/<tier>/client.toml` rather than materializing a copy under the env root.
> * `minds env activate production` is supported (runs against production from source); the other way to reach production is to *deactivate* (unset the vars) and run an installed/bundled build, which uses its bundled config — deactivating while running from source has no config file and fails.
> * `minds env activate` for `staging`/`production` auto-creates `~/.minds-<tier>/` (or `~/.minds/`) if missing (they're known tier names with in-repo config — no risk of typoed name); for dev env names it still fails when the dir doesn't exist and suggests `minds env deploy <name>`.
> * `minds env deploy` / `destroy` operate on the currently-activated env (no name argument) and fail when no env is activated; destroy additionally refuses hard-coded if the activated env is production or if nothing is set, as a safety mechanism in addition to other protections.
> * All deployment (dev, staging, production) flows through `minds env deploy` on the activated env — there is no longer a separate `scripts/deploy_*.sh <tier>` path; the only difference between tiers is which env is activated. The existing scripts and every CI workflow / runbook / changelog reference are ported within this same branch.
> * `minds env deactivate` is added, symmetric to `activate`; emits `unset MINDS_ROOT_NAME MNGR_HOST_DIR MNGR_PREFIX MINDS_CLIENT_CONFIG_PATH` for `eval`.
> * `minds env destroy` `rmdir`'s the now-empty `~/.minds-<name>/` after successful destroy so subsequent commands fail fast, and prints a hint to run `eval "$(minds env deactivate)"` in the shell.
> * Tier selection for the activated env is a hard-coded CLI mapping: env name `staging` → tier `staging`, env name `production` → tier `production`, everything else → tier `dev`.
> * Unactivated source runs (`uv run minds run` with no env vars set) refuse to start with "no env activated; run `minds env activate <name>` first"; the bundled-Electron path always passes `--config-file` explicitly (for prod, staging, beta, etc. builds) — explicit is better than implicit.
> * `minds env deploy` against `production` (and `staging`) requires a `--yes-i-mean-production` (or analogous per-tier) CLI flag; no interactive prompt.
> * The Electron build accepts an explicit `MINDS_CLIENT_CONFIG_BUNDLE=<path>` env var (the path of the non-secret `client.toml` to embed) and a second `MINDS_ROOT_NAME_BUNDLE=<minds(-<tier>)?>` env var; Electron startup reads both. `MINDS_BUILD_TIER` is dropped in favor of these explicit knobs.
> * The dev tier's static `apps/minds/imbue/minds/config/envs/dev/client.toml` is deleted entirely; only `dev/deploy.toml` (vault prefix, modal workspace, etc.) remains. Source runs require activation, so there's no dev-tier fallback to fall back to.
> * Admin commands (`mngr_imbue_cloud admin pool create ...`, etc.) likewise require an activated env; the tier is derived from the activated env name. No standalone `--tier` overrides.
> * Per-tier client.toml safety: the deploy writer for staging/production refuses to ever serialize anything other than the public URL fields into the committed in-repo file; secrets values are guaranteed by both data-type split (a `PublicClientEnvConfig` with no `secrets` field) and a runtime refusal at write time.

## Overview

- Today, every dev iteration shares one data root (`~/.devminds/`), and per-dev-env state piles up under `~/.devminds/envs/<name>.toml`. Each new dynamic dev env adds load to the same mngr profile, the same auth state, the same agent list — and the prod root (`~/.minds/`) is the only environment with its own isolated dir. This makes parallel experimentation messy and the distinction between "I'm iterating on env A" vs "env B" implicit.
- Pivot to **one data root per environment**: every env (dev, staging, dynamic per-dev) gets its own `~/.minds-<env-name>/` with its own mngr profile, agents, auth, config, and (for dev envs) secrets. Production keeps `~/.minds/` unchanged in shape.
- The shell must **activate** an env before running `minds`/`mngr` commands (`eval "$(minds env activate <name>)"` exports `MINDS_ROOT_NAME`, `MNGR_HOST_DIR`, `MNGR_PREFIX`, `MINDS_CLIENT_CONFIG_PATH`). Unactivated source runs refuse to start so nothing accidentally writes to the wrong root. `minds env deactivate` is the inverse.
- **All deployment converges on `minds env deploy`** for every tier — `scripts/deploy_remote_service_connector.sh` and `scripts/deploy_litellm.sh` are deleted in this branch; the only difference between deploying staging vs production vs a dev env is which env is active in the shell.
- Per-env on-disk state is split into a non-secret `client.toml` (committed in-repo for staging/production, written under the env root for dev envs) and a chmod-600 `secrets.toml` (only for dev envs; for staging/production the same values live in Vault). Hard guarantees prevent the in-repo file from ever holding secrets.

## Expected Behavior

### Activation / deactivation

- `minds env activate <name>` validates `<name>` against the existing env-name regex, then prints shell exports for `MINDS_ROOT_NAME=minds-<name>` (or `MINDS_ROOT_NAME=minds` when `<name>=production`), `MNGR_HOST_DIR=$HOME/.minds-<name>` (or `$HOME/.minds`), `MNGR_PREFIX=minds-<name>-` (or `minds-`), and `MINDS_CLIENT_CONFIG_PATH=<path-to-client.toml>`. Output is shell-sourceable; a leading `#` line shows the invocation hint.
- For `production` and `staging` (reserved names), `MINDS_CLIENT_CONFIG_PATH` points at the in-repo `apps/minds/imbue/minds/config/envs/<tier>/client.toml`. For every other name it points at `~/.minds-<name>/client.toml`.
- For `production` and `staging`, the activation call auto-creates `~/.minds/` or `~/.minds-staging/` if it doesn't exist (these are known good names — no chance of a typo). For any other name, activation refuses if `~/.minds-<name>/` doesn't exist and tells the operator to run `minds env deploy <name>` first.
- `minds env deactivate` prints `unset MINDS_ROOT_NAME MNGR_HOST_DIR MNGR_PREFIX MINDS_CLIENT_CONFIG_PATH` for `eval`. No file changes.
- Strictly an `eval`-sourced flow — no shell-function init layer is shipped.
- Inheritance: when `MINDS_ROOT_NAME` is set to a non-conforming value (e.g. a stale `devminds` left in a parent shell), the bootstrap treats it as unset, falls back to production (`~/.minds/`), and logs a warning naming the offending value.

### Listing

- `minds env list` globs `~/.minds*/` (i.e. `~/.minds/` plus every `~/.minds-*/`) and prints one row per dir. Production (`~/.minds/`) is rendered with a special name marker.
- Each row shows: env name, the resolved `MNGR_HOST_DIR`, the `client.toml` source (in-repo path for `staging`/`production`, under-root path for dev envs), connector URL (read from the resolved `client.toml`), and a marker for the currently-activated env if one matches.
- JSON / JSONL output formats are supported via the existing `--output-format` plumbing.

### Deploy and destroy

- `minds env deploy` takes no env-name argument; it reads the activated env. Refuses with a clear error when no env is active.
- Tier selection from env name is hard-coded: `production` → tier `production`, `staging` → tier `staging`, anything else → tier `dev`. The selected tier drives the Vault prefix, Modal workspace, and per-tier OAuth client IDs read from `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml`.
- Deploy behavior by tier:
  - **dev**: provisions the dev env's Modal env, Neon DB, SuperTokens app (existing flow), pushes per-env Modal Secrets, deploys both Modal apps. Writes `client.toml` (public URLs) and chmod-600 `secrets.toml` (Neon DSN, SuperTokens conn URI, SuperTokens API key) under `~/.minds-<name>/`.
  - **staging / production**: requires `--yes-i-mean-production` (or the analogous per-tier confirmation CLI flag) — refuses otherwise. Pushes Vault secrets to Modal Secrets and `modal deploy`s both apps. Writes **nothing** to disk: no `client.toml` update, no `secrets.toml`, no in-repo file change. The committed `apps/minds/imbue/minds/config/envs/<tier>/client.toml` remains the source of truth, updated only by hand on the rare occasions URLs change.
- `minds env destroy` takes no env-name argument; it reads the activated env. Hard-coded refusals:
  - No env activated → refuse.
  - Activated env name is `production` → refuse.
  - (Staging destroy is *allowed* under the same `--yes-i-mean-production`-style guard, but production cannot be destroyed through this CLI at all.)
- On a successful destroy, the empty `~/.minds-<name>/` directory is removed (`rmdir`) and a closing message reminds the operator to `eval "$(minds env deactivate)"` to clear their shell.

### Unactivated source runs

- `uv run minds run` (and any other CLI entry that loads the client config) refuses to start when no env is activated, with: "no env activated; run `minds env activate <name>` first". No silent fallback to a dev `client.toml`.
- This applies symmetrically to admin/dev recipes: `propagate_changes`, `forward-*-system-interface`, `minds-start`, etc. all require activation.

### Packaged-Electron behavior

- The Electron build reads two new build-time env vars: `MINDS_CLIENT_CONFIG_BUNDLE=<path>` (the non-secret `client.toml` to embed) and `MINDS_ROOT_NAME_BUNDLE=<minds(-<tier>)?>` (the on-disk root name the build should write to at runtime). Both are required for non-dev builds; `MINDS_BUILD_TIER` is removed.
- At runtime, the Electron main process exports `MINDS_ROOT_NAME=<bundled-root-name>` (and the derived `MNGR_*` vars) before launching `minds run --config-file <bundled-config-path>`. A production build uses `MINDS_ROOT_NAME=minds`; a beta build pointed at staging uses `MINDS_ROOT_NAME=minds-staging` (so its on-disk state lands in `~/.minds-staging/` and never collides with an installed prod build).
- The bundled-Electron path passes `--config-file` explicitly; there is no implicit fallback at any layer.

### Data files

- **Non-secret per-env config** (`client.toml`): connector URL, LiteLLM proxy URL, optional public toggles. Lives at `apps/minds/imbue/minds/config/envs/<tier>/client.toml` for `staging`/`production` (committed) and at `~/.minds-<env-name>/client.toml` for dev envs (chmod 0644 — no secrets allowed).
- **Per-env secrets** (`secrets.toml`): Neon DSN, SuperTokens connection URI, SuperTokens API key — values that `minds env deploy` needs on re-runs. **Only ever written for dev envs**, at `~/.minds-<env-name>/secrets.toml` (chmod 0600). Staging/production deploys read these same values from Vault.
- Hard guarantee: the data-type used to serialize the in-repo `client.toml` has no `secrets` field at all, and the deploy writer for staging/production explicitly refuses to write anywhere other than the public-only path. Two layers (typed and runtime) prevent a secret from ever landing in a committed file.

## Changes

### CLI

- `minds env activate <name>` — exports `MINDS_ROOT_NAME`, `MNGR_HOST_DIR`, `MNGR_PREFIX`, `MINDS_CLIENT_CONFIG_PATH`. Behaviour differs for reserved names (`production`, `staging`) vs dev envs as described in *Expected Behavior*.
- `minds env deactivate` — new. Prints the `unset` exports for the four vars.
- `minds env deploy` — drops the positional `<name>` argument; operates on activated env. Adds a `--yes-i-mean-production` (or analogous per-tier) confirmation flag, required for `staging` / `production`. Routes to dev-tier provisioning (writes `client.toml` + `secrets.toml`) or to tier-deploy (no disk writes) based on the activated env's tier.
- `minds env destroy` — drops the positional `<name>` argument; operates on activated env. Hard-refuses `production`. On success, `rmdir`s the now-empty env root and prints the deactivate hint.
- `minds env list` — re-implemented to glob `~/.minds*/` directly; rows now carry the resolved `MNGR_HOST_DIR`, the `client.toml` source path, and an "active" marker.

### Data model and on-disk layout

- Per-env directory layout becomes:
  - `~/.minds-<env-name>/client.toml` (dev envs only; non-secret)
  - `~/.minds-<env-name>/secrets.toml` (dev envs only; chmod 600)
  - everything else already living under the env root today (mngr profile, auth, agents, logs, etc.)
- `LocalDevEnvConfig` is split: a `PublicClientEnvConfig` with only the URL fields (used for staging/production in-repo files and for the dev `client.toml`) and a `DevEnvSecrets` model for the chmod-600 secrets file. The combined "one file with both" shape goes away.
- Single in-repo file per non-dev tier: `apps/minds/imbue/minds/config/envs/{staging,production}/client.toml` carry only public URLs. The dev tier's `client.toml` is deleted; `dev/deploy.toml` stays as the tier-shared deploy config.
- Deploy writer refuses to serialize any non-public field to a committed in-repo path; a ratchet/test asserts this.

### Env-var bootstrap

- `MINDS_ROOT_NAME` validation tightens to `minds(-<env-name>)?` shape; non-conforming values are treated as unset with a warning and the process continues with `~/.minds/` as the data root.
- The bootstrap that derives `MNGR_HOST_DIR` / `MNGR_PREFIX` no longer `setdefault`s for unset `MINDS_ROOT_NAME` callers that go through `minds run` / dev recipes — those callers must be activated. The bundled-Electron entry path explicitly exports the vars itself from the bundled build's two-knob config.
- The legacy `~/.devminds/` directory is no longer special — code does not read from or write to it. Docs note that operators can `rm -rf ~/.devminds/` when convenient.

### Deployment unification

- `scripts/deploy_remote_service_connector.sh`, `scripts/deploy_litellm.sh`, and `scripts/push_modal_secrets.py` are deleted (or fully absorbed). All flows go through `minds env deploy` on the activated env.
- CI workflows, runbooks, changelog references, and `apps/minds/docs/environments.md` are updated in the same branch to use `minds env activate` + `minds env deploy`.
- Per-tier deploy parameters (Vault prefix, Modal workspace, OAuth client IDs) remain in `apps/minds/imbue/minds/config/envs/<tier>/deploy.toml`; tier is selected by the hard-coded mapping from the activated env name.

### Recipes, scripts, and skills

- `justfile`: `devminds-start`, `forward-devminds-system-interface`, the dual `forward-{minds,devminds}-system-interface` pair, the `propagate-changes` recipe's `mngr_host_dir` defaulting, and any other recipe that hard-codes `~/.minds/` or `~/.devminds/` are replaced by env-agnostic versions that read the activated env from the shell and refuse with a clear error when unset.
- `apps/minds/scripts/propagate_changes`: drops the `MINDS_ROOT_NAME=${MINDS_ROOT_NAME:-minds}` default; requires activation.
- `.claude/skills/minds-dev-workflow/SKILL.md` and any other skill referencing `~/.devminds/` / `MINDS_ROOT_NAME=devminds` / `just devminds-start` are rewritten around `minds env activate <name>` + the generic recipes.
- Admin commands (`mngr_imbue_cloud admin pool create ...`, etc.) refuse to run without activation; the tier is sourced from the activated env name with no separate `--tier` override.

### Docs

- `apps/minds/docs/environments.md` — rewritten end-to-end around activation, the unified `minds env deploy` path, the in-repo vs under-root split, the bundled-Electron build env vars, and the "rm -rf ~/.devminds/" cleanup note.
- `apps/minds/docs/desktop-app.md` — update the data-directory section to enumerate the new file split and the activation requirement.
- `apps/minds/docs/workspace/getting_started.md` — refresh the dev iteration walkthrough to use activation rather than `MINDS_ROOT_NAME=devminds`.
- `apps/minds/docs/vault-setup.md` — update to reflect that staging/production deploys read Vault for *all* their secrets (no local fallback) and that dev envs do not push their per-env secrets to Vault.
