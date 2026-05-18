# Minds deploy safety overhaul

## Overview

- Collapse the two deploy paths (`deploy_dev_env`, `deploy_tier_env`) into a single `deploy_env` driven by four required `[lifecycle]` flags in each tier's `deploy.toml`. Every tier exercises the same code; bugs in any shared step get caught by any tier's CI.
- Shorten Modal app + function names so the full deployed hostname stays under DNS's 63-char limit for every realistic env name. Eliminates Modal's deterministic 6-hex truncation and the post-deploy URL-fix-up + connector redeploy.
- Make every deploy survivable: write one recover-target file at the start, mint timestamped Modal Secrets the deployed app pins itself to via `MINDS_DEPLOY_ID`, take a Neon named restore-point, capture the pre-deploy Modal app versions. On any failure the operator runs `minds env recover` to converge the cloud back to the captured target. Recover is idempotent and stage-agnostic.
- Lock the deploy CLI down: deploy refuses to start if a recover-target file exists, deploy can only run from the monorepo root, `MINDS_DEPLOY_ID` is mandatory in the deployed app's env (hard fail at module load, no fallback).
- Keep the last 10 timestamped Modal Secrets per `<service>-<tier>`; let Neon's PITR window age out restore-points automatically; reuse Modal's native app version history.
- All changes are greenfield (the existing per-env work has not landed in any live tier). No backwards compatibility, no migration shims.

## Expected Behavior

### Operator-facing CLI

- `minds env deploy` (with any tier activated, dev or shared) runs the unified deploy path. Behavior diverges only via the four `[lifecycle]` flags.
- `minds env deploy` refuses to start if `.minds-deploy-recover-target.json` exists at the monorepo root. Error message names the file and points at `minds env recover`.
- `minds env deploy` refuses to start if invoked from outside the monorepo (walks up from CWD looking for `apps/`; errors if no marker found).
- `minds env recover` (new command) reads the recover-target file, runs every reversal step (each individually idempotent), logs each step's success/failure, and deletes the file on full success. Re-runnable until success. Refuses to run if the file does not exist.
- Every other `minds env *` command (`activate`, `deactivate`, `list`, `destroy`) refuses outright if the recover-target file exists. Operator must `minds env recover` (or manually clear the file for a known-stale entry) before any other minds operation can proceed.
- `minds env destroy` is unchanged: it remains run-to-completion-with-best-effort-cleanup and is its own recovery (re-run after a partial failure).
- `minds env activate` / `list` are otherwise unchanged.

### Deploy flow (single, unified)

For any tier:

1. **Preflight** — read-only validation. No external mutation allowed.
2. **Capture pre-deploy state + write recover-target file** — `modal app history --json` for each of the two apps, `POST` a Neon restore-point named `pre-deploy-<deploy_id>`, write the recover-target file atomically (`tempfile + fsync + rename`).
3. **Push new timestamped Modal Secrets** — one `<service>-<tier>-<deploy_id>` per service named in `deploy.toml`'s `[secrets].services`, into the appropriate Modal env.
4. **Run migrations** — pool-hosts `schema_migrations`-driven runner against the env's `host_pool` DB; Prisma migration against `litellm_cost`.
5. **`modal deploy` both apps** — into the appropriate Modal env, with `MNGR_DEPLOY_ENV=<tier>` + `MINDS_DEPLOY_ID=<id>` threaded into the subprocess env. The apps read both at module load and call `Secret.from_name(f"<svc>-<tier>-<deploy_id>")`. The `modal deploy` stdout is parsed for the deployed URLs and asserted to match the up-front-computed URLs (`per_env_connector_url`, `per_env_litellm_proxy_url`).
6. **Health check** — poll `GET <connector_url>/health/liveness` + `GET <litellm_proxy_url>/health/liveness` every 2s for up to 60s. Both must hit 200 within the window. Both endpoints are no-auth liveness probes that return a tiny JSON body when the process is up. (LiteLLM's `/health` requires a master key and pings configured models -- much heavier than we want.)
7. **Cleanup** — keep the last 10 timestamped Modal Secrets per `<service>-<tier>` (delete older ones); delete the recover-target file. Cleanup failures are logged but never trigger rollback.

On any exception during steps 2-6, the deploy exits non-zero with `"deploy failed at step N: <error>; run `minds env recover` to roll back"`. No inline rollback.

### Lifecycle flags (in `deploy.toml`)

| Flag | dev | staging | production |
| --- | --- | --- | --- |
| `creates_resources` | `true` | `false` | `false` |
| `modal_env_strategy` | `"per_env"` | `"shared"` | `"shared"` |
| `writes_local_state` | `true` | `false` | `false` |
| `tracks_generation` | `false` | `true` | `true` |

- `creates_resources=true`: deploy provisions Modal env, Neon project, SuperTokens app. `false`: deploy reads them out of Vault and refuses to call any project-create/delete endpoint.
- `modal_env_strategy="per_env"`: every deploy targets Modal env named after the activated dev env. `"shared"`: every deploy targets the deploy.toml `modal_env` (`main` by convention).
- `writes_local_state=true`: deploy writes `~/.minds-<env>/client.toml` (chmod 0644) + `secrets.toml` (chmod 0600). `false`: deploy writes nothing local (URLs derive from the committed in-repo `client.toml`).
- `tracks_generation=true`: a tier-wide generation id is minted on first deploy + exposed via the litellm-connector's `/generation` route; destroy bumps it. Production destroy is hard-refused at the CLI today, so production's generation id is effectively immutable for the lifetime of the tier. The flag is set to `true` for production purely for parity with staging.

### Naming

- Modal workspaces: `minds-dev`, `minds-staging`, `minds-production`.
- Modal apps: `rsc-<tier>` (was `remote-service-connector-<tier>`), `llm-<tier>` (was `litellm-proxy-<tier>`).
- Modal function names (the `@app.function(name=...)` strings): `api` for the connector's FastAPI app (was `fastapi_app`), `proxy` for the LiteLLM app (was `litellm_app`).
- Deployed URL example (dev tier, env `dev-josh-1`): `https://minds-dev-dev-josh-1--rsc-api.modal.run`. Hostname length = `minds-dev` (9) + `-` (1) + `dev-josh-1` (10) + `--rsc-api` (9) = 29 chars. Well under 63.
- `DevEnvName` enforces max length 40 chars at construction. Budget derivation: `63 (DNS) - 13 (longest planned workspace 'minds-staging') - 1 (separator) - 9 ('--rsc-api')`.
- **Python module / package / vault-entry names do NOT change.** The rename is purely in the strings passed to `modal.App(name=...)` and `@app.function(name=...)`.

### Modal Secret lifecycle

- Each deploy mints `MINDS_DEPLOY_ID` = `YYYYMMDDTHHMMSSZ` (UTC). Lex sort = chron sort.
- Each of the services in `deploy.toml`'s `[secrets].services` gets pushed as a NEW Modal Secret named `<service>-<tier>-<deploy_id>` (never overwrites). Single push pass — no second pass + redeploy (URLs are computable up-front under the new naming).
- The deployed Modal apps read `MNGR_DEPLOY_ENV` + `MINDS_DEPLOY_ID` at module load and pass them into every `Secret.from_name(f"<svc>-<tier>-<deploy_id>")` call.
- If `MINDS_DEPLOY_ID` is missing at module load, the app raises `DeployIdMissingError` and the Modal deploy fails. No fallback path — manual `modal deploy` outside of `minds env deploy` is unsupported.
- Cleanup at end of every successful deploy walks `modal secret list --env=<env> --json`, regexes the `<svc>-<tier>-<id>` suffix, sorts by id, deletes everything older than the most-recent 10 per `<svc>-<tier>`. Cleanup failures never trigger rollback.

### Neon snapshot / restore

- Every deploy creates a **Neon child branch** named `pre-deploy-<deploy_id>` off the project's default branch BEFORE touching anything else (after preflight, as part of step 2). Branch creation is lazy + copy-on-write, so the snapshot itself is near-free until writes diverge.
- Recover restores the parent branch from this snapshot branch via `POST .../branches/<id>/restore` (Neon's instant-restore endpoint), captured pre-rollback state under `pre-rollback-<deploy_id>` so the operator can inspect the broken state via the Neon console if needed. Successful deploy deletes the snapshot branch best-effort (cluttering the project otherwise); on any failure between snapshot creation and successful deploy completion, the snapshot branch stays so `recover` can use it.
- Recover always restores (never gates on "did migrations actually run"). Neon's branch restore is a near-no-op when nothing has changed since the snapshot.
- **Note:** an earlier draft of this spec described "Neon named restore-points." The implementation pivoted to child branches because Neon's named-restore-point API is org-tier-gated and not available on the dev tier's plan; child branches give us the same instant-restore guarantee on every Neon plan. The recover-target schema and `recover_env` step ordering reflect the branch-based approach.

### Migration tracking

- Replace today's "replay every `.sql` with `IF NOT EXISTS` guards" with a real tracking table:
  - Table: `schema_migrations(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())`.
  - `version` = literal filename (e.g. `001_attributes_jsonb.sql`).
  - The runner lists files in `apps/remote_service_connector/migrations/*.sql`, sorts lex, applies any whose name is not yet in the table; inserts the row only after the `.sql` runs successfully.
  - On first run after the change, the table doesn't exist. Detect that, create it, AND backfill rows for every currently-existing migration file (assumed already applied since they all used `IF NOT EXISTS`).
- New migrations (added after this change) should NOT use `IF NOT EXISTS` guards — the tracking table is the source of truth.
- LiteLLM uses Prisma's `_prisma_migrations` table the same way; we don't replicate the tracking, we just trust Prisma.
- The migration runner code lives in a new module `apps/minds/imbue/minds/envs/migrations.py` (reusable across the pool-hosts + litellm paths). The deploy's migration step now lives entirely in this new module; `neon_db.apply_pool_hosts_schema` is removed (its logic moves to `migrations.py`).

### Recover-target file

- Path: `.minds-deploy-recover-target.json` at the monorepo root.
- Created atomically (`tempfile + fsync + rename`) after preflight succeeds and after the Neon snapshot branch is created, BEFORE any other external mutation.
- Schema (all fields required):
  ```json
  {
    "deploy_id": "20260517T143022Z",
    "env_name": "dev-josh-1",
    "tier": "dev",
    "modal_env": "dev-josh-1",
    "modal_workspace": "minds-dev",
    "vault_path_prefix": "secrets/minds/dev",
    "neon_project_id": "raspy-lake-12345678",
    "neon_branch_id": "br-old-fire-akygmp0x",
    "neon_snapshot_branch_id": "br-icy-truth-akf2v98n",
    "app_versions_to_restore": {
      "rsc-dev": "v17",
      "llm-dev": "v23"
    }
  }
  ```
- `app_versions_to_restore` values may be `null` for a first-ever deploy with no prior version; recover logs a warning + skips Modal rollback for that app (Neon restore + secret cleanup still run).
- The file is gitignored via `.minds-deploy-*.json` in the root `.gitignore`.
- Successful recover deletes the file. Successful deploy deletes the file as the last step before exit.

### `minds env recover` semantics

- Reads `.minds-deploy-recover-target.json`. Refuses to run if missing.
- Runs every reversal step in order, regardless of which stage the failed deploy reached:
  1. For each app in `app_versions_to_restore`: `modal app rollback <app> <version>` (skip if value is null; log warning).
  2. Neon: restore the parent branch from `neon_snapshot_branch_id` via `restore_branch_from_snapshot` (which calls Neon's `POST .../branches/<id>/restore`), capturing the pre-restore state under `pre-rollback-<deploy_id>` for forensic inspection.
  3. For each service in `deploy.toml`'s `[secrets].services`: `modal secret delete <svc>-<tier>-<deploy_id> --env=<modal_env>`. Idempotent (treats "not found" as success — see existing `delete_modal_secret`).
  4. Delete `.minds-deploy-recover-target.json`.
- Step failures are logged but recover proceeds through every step (best-effort across the whole flow). Exits non-zero if any step failed; operator re-runs.
- No plan-then-apply gesture — recover just does it. Logs the captured-target JSON at the start so the operator sees what's being restored.
- Stale recover-target (env actually gone): the rollback calls fail naturally with "not found"; recover surfaces those errors. Operator deletes the file manually after deciding it's no longer relevant.

### Health check

- Polls every 2s for up to 60s.
- Per-attempt HTTP timeout: 10s.
- Endpoints: `<connector_url>/health/liveness` (expects 200; connector's no-auth liveness probe), `<litellm_proxy_url>/health/liveness` (expects 200; LiteLLM's no-auth liveness probe).
- Categorization:
  - **Success**: both endpoints return 200 with the expected shape at least once during the window. Stop polling at first joint success.
  - **Transient (continue polling)**: connection refused, connection reset, DNS not resolving, socket timeout, HTTP 502/503/504 with empty body, any HTTP 5xx during the first 10 seconds (cold-boot tolerance).
  - **Definitive (fail immediately)**: HTTP 4xx, HTTP 5xx with a non-empty body after the cold-boot window, malformed response, HTTP 200 whose body doesn't parse as the expected JSON shape.
- Failure ⇒ exit non-zero with the same "run `minds env recover`" guidance.

### Preflight

Runs before any external mutation. Any failure aborts before the recover-target file is written:

- `.minds-deploy-recover-target.json` does not exist at the monorepo root.
- Every Vault entry named in `deploy.toml`'s `[secrets].services` is readable.
- `modal whoami` succeeds and its reported workspace matches `deploy.toml`'s `modal_workspace`.
- Neon API token (read from `secrets/<tier_vault_prefix>/neon-admin.NEON_API_TOKEN`) has snapshot + restore-point scope. Probe via `POST /projects/{id}/branches/{id}/restore` with a noop / dry-run shape, or fall back to `GET /projects/{id}` + an explicit feature-flag check from the project metadata if Neon doesn't expose a dry-run.
- For `creates_resources=true` only: SuperTokens API key (from `secrets/<tier_vault_prefix>/supertokens.SUPERTOKENS_API_KEY`) authenticates against the configured core URL.
- Migration files exist on disk and the runner can parse them.
- `modal app history --json <app>` succeeds for each of the two apps (or returns "no such app" cleanly, in which case the version is captured as `null`). The successful versions are stashed for use in the recover-target file.

## Implementation Plan

### Files to add

- `apps/minds/imbue/minds/envs/deploy.py` — the new unified `deploy_env(...)` orchestration. Replaces `deploy_dev_env` + `deploy_tier_env` from `provisioning.py`. Owns the step ordering (preflight → capture → push secrets → migrate → modal deploy → health check → cleanup). Reads the `[lifecycle]` flags off the resolved `DeployEnvConfig` and dispatches per-step provider behavior.
- `apps/minds/imbue/minds/envs/recover.py` — `recover_env()`, `read_recover_target()`, `write_recover_target_atomic()`, `delete_recover_target()`. The reversal-step orchestrator (steps 1-4 above). Stage-agnostic.
- `apps/minds/imbue/minds/envs/migrations.py` — `ensure_schema_migrations_table()`, `list_pending_pool_hosts_migrations(dsn) -> list[Path]`, `apply_pool_hosts_migrations(dsn, parent_cg)`, `backfill_schema_migrations_if_needed(dsn, parent_cg)`, `check_litellm_prisma_migrations_table_exists(dsn) -> bool`. Replaces `neon_db.apply_pool_hosts_schema`.
- `apps/minds/imbue/minds/envs/health_check.py` — `await_apps_healthy(connector_url, litellm_proxy_url, *, max_seconds=30, poll_interval=2.0, per_attempt_timeout=3.0, cold_boot_seconds=10.0) -> None`. Raises `HealthCheckFailedError` on definitive failure or timeout.
- `apps/minds/imbue/minds/envs/secret_lifecycle.py` — `make_deploy_id() -> str`, `timestamped_secret_name(service, tier, deploy_id) -> str`, `parse_timestamped_secret_name(name) -> tuple[service, tier, deploy_id] | None`, `list_active_per_tier_secrets(modal_env, tier, cg) -> list[str]`, `gc_old_per_tier_secrets(modal_env, tier, keep_last=10, cg) -> None`. Owns the naming convention end-to-end.
- `apps/minds/imbue/minds/envs/preflight.py` — `run_preflight(...) -> PreflightSummary`. Returns a frozen summary that includes the captured pre-deploy app versions so step 2 doesn't have to re-query Modal.
- `apps/minds/imbue/minds/envs/lifecycle.py` — `DeployLifecycleConfig` frozen-model + `ModalEnvStrategy` enum (PER_ENV / SHARED, `UpperCaseStrEnum`-pattern). Imported by `config/data_types.py`.
- `apps/minds/imbue/minds/cli/recover.py` — the `minds env recover` CLI entry point. Thin wrapper around `recover.recover_env()`.
- `apps/minds/imbue/minds/envs/deploy_test.py` — unit tests for the unified deploy dispatch (all four lifecycle-flag tiers, fakes injected).
- `apps/minds/imbue/minds/envs/recover_test.py` — unit tests for recover target serialization + idempotent reversal-step convergence.
- `apps/minds/imbue/minds/envs/migrations_test.py` — unit tests for the schema_migrations runner + backfill logic.
- `apps/minds/imbue/minds/envs/health_check_test.py` — unit tests for the polling state machine + error categorization (faked httpx client).
- `apps/minds/imbue/minds/envs/secret_lifecycle_test.py` — unit tests for naming + GC math.
- `apps/minds/imbue/minds/envs/preflight_test.py` — unit tests for each preflight check (all green, each one red in isolation).
- `apps/minds/imbue/minds/envs/test_deploy_and_recover.py` — integration test (no marker; runs in default offload) that exercises the full deploy + induced-failure + recover cycle against in-memory simulator providers (each provider's fake tracks its own state across calls; an injected failure flag at any step triggers recover; recover converges back to captured target).

### Files to modify

- `apps/minds/imbue/minds/envs/provisioning.py` — delete entirely. Its module-level constants (`MINDS_ENV_NAME_KEY`, `_PROVIDER_ERRORS`) move to `deploy.py`. `Providers` / `ProviderCredentials` frozen models also move to `deploy.py` (or to a sibling `providers/__init__.py`-adjacent location; final placement TBD during implementation). `destroy_env` moves to `apps/minds/imbue/minds/envs/destroy.py` unchanged in behavior (just relocated to give `deploy.py` a focused home). The existing `_best_effort_rollback` + `_ROLLBACK_TABLE` are deleted, NOT moved.
- `apps/minds/imbue/minds/envs/per_env_deploy.py` — delete entirely. Its functions get split:
  - `ensure_modal_env`, `deploy_litellm_proxy`, `deploy_remote_service_connector`, `stop_modal_app`, `delete_modal_secret`, `per_env_secret_services`, `compute_per_env_overrides`, `build_per_env_secret_values`, `push_per_env_modal_secret` → move to `apps/minds/imbue/minds/envs/providers/modal_apps.py` (new).
  - `per_env_connector_url`, `per_env_litellm_proxy_url`, `_repo_root`, `_litellm_app_file`, `_connector_app_file` → move to `apps/minds/imbue/minds/envs/repo_layout.py` (new). Renamed: drop the `per_env_` prefix from URL helpers since they now apply to every tier.
  - The URL functions get updated for the new naming: `f"https://{workspace}-{name}--rsc-api.modal.run"` and `f"https://{workspace}-{name}--llm-proxy.modal.run"` for per-env tiers; `f"https://{workspace}--rsc-{tier}-api.modal.run"` and `f"https://{workspace}--llm-{tier}-proxy.modal.run"` for shared tiers. The two formulas pick the right path based on `lifecycle.modal_env_strategy`. Function-name strings (`api`, `proxy`) match the new `@app.function(name=...)` strings.
- `apps/minds/imbue/minds/envs/providers/neon_db.py`:
  - Add `create_named_restore_point(project_id, branch_id, restore_point_name, api_token) -> None` — calls Neon `POST /projects/{id}/branches/{id}/restore_points` (or the equivalent Neon endpoint; final URL TBD during implementation against Neon docs).
  - Add `restore_branch_to_named_restore_point(project_id, branch_id, restore_point_name, api_token) -> None` — calls `POST /projects/{id}/branches/{id}/restore`.
  - Add `verify_neon_token_has_restore_scope(project_id, branch_id, api_token) -> None` — preflight probe; raises `NeonInsufficientScopeError` on failure.
  - Remove `apply_pool_hosts_schema` (moves to `envs/migrations.py`).
  - `create_neon_project` / `delete_neon_project` are unchanged in behavior, but now only called when `lifecycle.creates_resources=true`. Add a runtime assertion in both functions to enforce this (defense-in-depth on top of the deploy.py dispatch).
- `apps/minds/imbue/minds/envs/primitives.py` — `DevEnvName` adds a max-length-40 constraint at construction; the new limit is documented in the docstring + tested. The `dev-` prefix enforcement stays.
- `apps/minds/imbue/minds/config/data_types.py`:
  - New `DeployLifecycleConfig(FrozenModel)` with fields `creates_resources: bool`, `modal_env_strategy: ModalEnvStrategy`, `writes_local_state: bool`, `tracks_generation: bool` (all required, no defaults).
  - `DeployEnvConfig` gets a new required `lifecycle: DeployLifecycleConfig` field.
  - Validator on `DeployEnvConfig` raises if `lifecycle.modal_env_strategy == PER_ENV` and `modal_env` was set to anything other than the default sentinel (mutually exclusive).
- `apps/minds/imbue/minds/config/envs/dev/deploy.toml`, `staging/deploy.toml`, `production/deploy.toml`:
  - Add the `[lifecycle]` block per the table above.
  - Rename `modal_workspace = "imbue-minds-dev"` → `"minds-dev"` (and `"minds-staging"`, `"minds-production"` for the respective tiers — the latter two were `"CHANGE_ME"` before).
- `apps/minds/imbue/minds/cli/env.py`:
  - Collapse the dev-vs-tier dispatch in `deploy_command()` to call `deploy_env(...)` for all tiers.
  - Add the recover-target-file existence check at the top of `activate`, `deactivate`, `deploy`, `destroy`, `list` — refuse outright if the file exists.
  - Register the new `recover` subcommand.
- `apps/modal_litellm/app.py`:
  - Rename: `app = modal.App(name=f"litellm-proxy-{_DEPLOY_ENV}", ...)` → `name=f"llm-{_DEPLOY_ENV}"`.
  - Rename the function: the `@app.function(name="litellm_app")` (or implicit name) becomes `@app.function(name="proxy")`.
  - At module load, read `MINDS_DEPLOY_ID = os.environ["MINDS_DEPLOY_ID"]` (raise `DeployIdMissingError` if missing).
  - Update every `modal.Secret.from_name(f"litellm-{_DEPLOY_ENV}")` → `modal.Secret.from_name(f"litellm-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}")`.
- `apps/remote_service_connector/imbue/remote_service_connector/app.py`:
  - Rename: `app = modal.App(name=f"remote-service-connector-{_DEPLOY_ENV}", ...)` → `name=f"rsc-{_DEPLOY_ENV}"`.
  - Rename the FastAPI function: `@app.function(name="fastapi_app")` → `name="api"`.
  - At module load, read `MINDS_DEPLOY_ID = os.environ["MINDS_DEPLOY_ID"]` (raise `DeployIdMissingError` if missing).
  - Update every `modal.Secret.from_name(f"<svc>-{_DEPLOY_ENV}")` → `modal.Secret.from_name(f"<svc>-{_DEPLOY_ENV}-{_MINDS_DEPLOY_ID}")` for each of the 6 secrets the connector mounts.
  - The existing error path that mentions "supertokens-{_DEPLOY_ENV} Modal secret" should be updated to include the deploy id in its message.
- `apps/remote_service_connector/migrations/*.sql` — leave existing 4 files unchanged (they used `IF NOT EXISTS` and are assumed already-applied on first run after the change; the backfill in `migrations.py` records them as applied). New migrations land WITHOUT `IF NOT EXISTS` guards.
- `apps/minds/imbue/minds/errors.py` — add `DeployIdMissingError`, `HealthCheckFailedError`, `RecoverFailedError`, `PreflightFailedError`, `NeonInsufficientScopeError` (each inheriting from `MindError` and the closest matching builtin).
- `.gitignore` (repo root) — add `.minds-deploy-*.json`.

### Key class / function signatures (new)

```python
# apps/minds/imbue/minds/envs/lifecycle.py
class ModalEnvStrategy(UpperCaseStrEnum):
    PER_ENV = auto()
    SHARED = auto()

class DeployLifecycleConfig(FrozenModel):
    creates_resources: bool
    modal_env_strategy: ModalEnvStrategy
    writes_local_state: bool
    tracks_generation: bool

# apps/minds/imbue/minds/envs/recover.py
class RecoverTarget(FrozenModel):
    deploy_id: DeployId
    env_name: str
    tier: str
    modal_env: str
    modal_workspace: str
    vault_path_prefix: str
    neon_project_id: str
    neon_restore_point_name: str
    app_versions_to_restore: dict[str, str | None]

def write_recover_target_atomic(target: RecoverTarget, *, repo_root: Path) -> Path: ...
def read_recover_target(repo_root: Path) -> RecoverTarget: ...   # raises RecoverTargetMissingError
def delete_recover_target(repo_root: Path) -> None: ...
def recover_env(*, providers: Providers, parent_cg: ConcurrencyGroup) -> None: ...

# apps/minds/imbue/minds/envs/preflight.py
class PreflightSummary(FrozenModel):
    deploy_id: DeployId
    app_versions: dict[str, str | None]   # captured pre-deploy
    neon_project_id: str
    neon_branch_id: str

def run_preflight(
    *, name: DevEnvName | None, tier: str, deploy_config: DeployEnvConfig,
    credentials: ProviderCredentials, providers: Providers, repo_root: Path,
    parent_cg: ConcurrencyGroup,
) -> PreflightSummary: ...

# apps/minds/imbue/minds/envs/deploy.py
def deploy_env(
    *, name: DevEnvName | None, tier: str, deploy_config: DeployEnvConfig,
    credentials: ProviderCredentials, providers: Providers,
    parent_cg: ConcurrencyGroup,
) -> DeployedEnv: ...

# apps/minds/imbue/minds/envs/health_check.py
def await_apps_healthy(
    connector_url: AnyUrl, litellm_proxy_url: AnyUrl,
    *, max_seconds: float = 30.0, poll_interval: float = 2.0,
    per_attempt_timeout: float = 3.0, cold_boot_seconds: float = 10.0,
) -> None: ...

# apps/minds/imbue/minds/envs/secret_lifecycle.py
class DeployId(NonEmptyStr): ...   # `YYYYMMDDTHHMMSSZ` format; validates at construction
def make_deploy_id(now: datetime | None = None) -> DeployId: ...
def timestamped_secret_name(service: ServiceName, tier: str, deploy_id: DeployId) -> str: ...
def parse_timestamped_secret_name(name: str) -> tuple[ServiceName, str, DeployId] | None: ...
def gc_old_per_tier_secrets(*, modal_env: str, tier: str, services: tuple[ServiceName, ...],
                            keep_last: int = 10, cg: ConcurrencyGroup) -> None: ...

# apps/minds/imbue/minds/envs/migrations.py
def ensure_schema_migrations_table(dsn: SecretStr, *, cg: ConcurrencyGroup) -> None: ...
def backfill_schema_migrations_if_needed(dsn: SecretStr, *, migrations_dir: Path, cg: ConcurrencyGroup) -> None: ...
def list_pending_pool_hosts_migrations(dsn: SecretStr, *, migrations_dir: Path, cg: ConcurrencyGroup) -> list[Path]: ...
def apply_pool_hosts_migrations(dsn: SecretStr, *, migrations_dir: Path, cg: ConcurrencyGroup) -> None: ...

# apps/minds/imbue/minds/envs/providers/neon_db.py (additions)
def create_named_restore_point(project_id: str, branch_id: str, name: str,
                                *, api_token: SecretStr) -> None: ...
def restore_branch_to_named_restore_point(project_id: str, branch_id: str, name: str,
                                          *, api_token: SecretStr) -> None: ...
def verify_neon_token_has_restore_scope(project_id: str, branch_id: str,
                                         *, api_token: SecretStr) -> None: ...
```

### Providers bundle changes

`Providers` (the injectable bundle) gains:

- `create_named_restore_point: CreateNeonRestorePointFn`
- `restore_branch_to_named_restore_point: RestoreNeonBranchFn`
- `verify_neon_token_has_restore_scope: VerifyNeonScopeFn`
- `get_modal_app_versions: GetModalAppVersionsFn` — wraps `modal app history --json`
- `modal_app_rollback: ModalAppRollbackFn` — wraps `modal app rollback <name> <version>`
- `await_apps_healthy: AwaitAppsHealthyFn` — wraps the health-check poller (factored out so tests can inject controlled time)
- `read_vault_value: ReadVaultValueFn` — already exists indirectly; surface it on Providers explicitly so preflight can read without duplicating Vault-client construction

The existing `push_per_env_modal_secret` signature gains a `deploy_id` parameter so naming stays centralized.

## Implementation Phases

Each phase is independently shippable and leaves the deploy in a working state.

### Phase 1 — Naming + URL determinism

Goal: dev deploys no longer need the second-pass secret update + connector redeploy. No behavior change to recover/safety.

- Rename Modal workspaces (`imbue-minds-dev` → `minds-dev`); rename `modal.App(name=...)` + `@app.function(name=...)` strings on both apps.
- Update `per_env_connector_url` / `per_env_litellm_proxy_url` (renamed `connector_url_for` / `litellm_proxy_url_for`, parameterized on lifecycle strategy).
- Delete the second-pass secret push + connector redeploy phase from `deploy_dev_env` (keep both deploy functions for now — Phase 2 collapses them).
- Add the URL-match-assertion at the end of each `modal deploy` call.
- Enforce `DevEnvName` max length 40 chars; update `DevEnvName` test fixtures that violate.
- Update `apps/minds/imbue/minds/config/envs/*/deploy.toml` for the new workspace names.
- Update README / docs that reference the old names (just the immediate-vicinity callouts; broader doc cleanup later).

**Test:** existing unit tests pass after rename; manual dev `minds env deploy` works end-to-end with no truncation + no redeploy phase.

### Phase 2 — Unified `deploy_env` driven by `[lifecycle]` flags

Goal: one deploy function, one destroy function. No new safety machinery yet.

- Add `DeployLifecycleConfig` + `ModalEnvStrategy` to `data_types.py` and as a required field on `DeployEnvConfig`.
- Add the `[lifecycle]` block to each tier's committed `deploy.toml`.
- Write `apps/minds/imbue/minds/envs/deploy.py` with the unified `deploy_env(...)`. Match today's behavior step-for-step for each tier — the only difference is which code path runs based on flags vs. which function the CLI called.
- Move `destroy_env` to `apps/minds/imbue/minds/envs/destroy.py` (no behavior change).
- Delete `apps/minds/imbue/minds/envs/provisioning.py`.
- Move per-env-deploy helpers into `providers/modal_apps.py` + `repo_layout.py` as described above.
- Delete `apps/minds/imbue/minds/envs/per_env_deploy.py`.
- Delete the inline rollback (`_best_effort_rollback`, `_ROLLBACK_TABLE`) entirely. Mid-create failures will now leak resources until the operator re-runs deploy — acceptable because Phase 3 introduces recover.
- CLI: collapse the dev-vs-tier dispatch in `cli/env.py`.

**Test:** unit tests for each lifecycle-flag combination's dispatch through `deploy_env`. Manual dev deploy + destroy continues to work; manual staging deploy (against a throwaway staging tier in a sandbox workspace) goes through the new code path.

### Phase 3 — Timestamped secrets + `MINDS_DEPLOY_ID` pinning

Goal: every deploy mints fresh secrets; the deployed app pins to that deploy's secrets via `MINDS_DEPLOY_ID`.

- Add `secret_lifecycle.py` (`DeployId`, `make_deploy_id`, naming + GC helpers).
- `deploy.py` threads `MINDS_DEPLOY_ID` into the subprocess env of every `modal deploy` / `modal run` invocation and uses timestamped names for every `push_per_env_modal_secret` call.
- Both Modal app files (`apps/modal_litellm/app.py`, `apps/remote_service_connector/imbue/remote_service_connector/app.py`) read `MINDS_DEPLOY_ID` at module load and build `Secret.from_name(f"<svc>-<tier>-<deploy_id>")`. Hard fail if missing.
- Cleanup step at end of every successful deploy: `gc_old_per_tier_secrets(keep_last=10)`.
- Cleanup is best-effort — failures are logged but don't fail the deploy.

**Test:** unit tests for naming + GC math. Manual dev deploy creates a new secret with the timestamp suffix; cleanup keeps the right set.

### Phase 4 — Pool-hosts `schema_migrations` table + migration runner extraction

Goal: real migration tracking, decoupled from the snapshot/restore decision.

- Add `migrations.py` with `ensure_schema_migrations_table`, `backfill_schema_migrations_if_needed`, `list_pending_pool_hosts_migrations`, `apply_pool_hosts_migrations`.
- `deploy.py`'s migration step calls `apply_pool_hosts_migrations` instead of `neon_db.apply_pool_hosts_schema`.
- Remove `neon_db.apply_pool_hosts_schema` (its callers are deploy + (formerly) `create_neon_project`; deploy now handles it directly post-Neon-create).
- New migrations added going forward MUST NOT use `IF NOT EXISTS` guards.

**Test:** unit tests for table creation, backfill, pending-detection. Manual deploy against a freshly-created dev env runs the backfill cleanly; subsequent deploys treat the migrations as already-applied.

### Phase 5 — Neon restore-point creation + recover-target file + `minds env recover`

Goal: the operator can run `minds env recover` after any deploy failure and get back to the pre-deploy state.

- Extend `neon_db.py` with `create_named_restore_point` + `restore_branch_to_named_restore_point` + `verify_neon_token_has_restore_scope`.
- Add `recover.py` with `RecoverTarget`, atomic file IO, and `recover_env()`.
- Add `preflight.py` with the full preflight set; integrate `verify_neon_token_has_restore_scope` + `modal app history --json` capture.
- Add `apps/minds/imbue/minds/cli/recover.py` and wire `minds env recover` into the CLI.
- Update every `minds env *` command to refuse if the recover-target file exists.
- `.gitignore` update.
- Modify `deploy.py` to: run preflight → create Neon restore-point + write recover-target file → existing deploy flow (now without inline error handling) → delete recover-target file on success. On any exception, exit with "run `minds env recover`" message.

**Test:** unit tests for `RecoverTarget` serialization, preflight checks (each one red in isolation), recover step idempotence. Integration test: simulate failure at each step + assert recover converges back to captured target.

### Phase 6 — Health check + post-deploy gate

Goal: a healthy-app gate at the end of every deploy, with rollback on failure.

- Add `health_check.py` with `await_apps_healthy`.
- `deploy.py` calls `await_apps_healthy` after step 5. Health-check failure throws an exception that goes through the same "run `minds env recover`" exit path as any other deploy failure.

**Test:** unit tests for the polling state machine (faked httpx client; cover transient retries + definitive immediate-fail + cold-boot tolerance + success-on-second-poll).

### Phase 7 — Generation tracking parity for production + cleanups

Goal: production gets `tracks_generation=true`; final doc / changelog updates.

- Update `production/deploy.toml`: `[lifecycle].tracks_generation = true`.
- Verify production destroy stays hard-refused at the CLI (no behavior change since destroy never runs).
- Update `apps/minds/README.md` + `apps/minds/imbue/minds/envs/README.md` (if it exists) to describe the new deploy / recover flow.
- Add a changelog entry at `changelog/mngr-env-testing.md`.

**Test:** existing tests pass; doc reads correctly.

## Testing Strategy

### Unit tests (`_test.py`, all use faked providers)

- `lifecycle_test.py` — `DeployLifecycleConfig` validation (all four flags required, no defaults, enum coercion).
- `deploy_test.py` — for each of the four tier-shaped configs (dev / staging / production / a synthetic 4th to stress flag independence), assert `deploy_env(...)` calls the right providers in the right order. Faked providers raise to test exception propagation.
- `recover_test.py` — `RecoverTarget` JSON round-trip; atomic write is actually atomic (write goes via tempfile + rename; partial write doesn't leak); `recover_env` idempotence (running it twice from the same captured state produces the same provider call sequence); per-step failure handling (each step's failure logged but doesn't abort subsequent steps).
- `preflight_test.py` — all-green happy path; each individual check failing in isolation produces the right error class; preflight aborts BEFORE the recover-target file is written.
- `health_check_test.py` — faked httpx client with controllable time. Cover: both healthy on first poll → success; transient connection refused → retry → success; HTTP 404 → immediate definitive fail; HTTP 503 with empty body during cold-boot → retry → success; cold-boot timeout after 30s → fail; one healthy + one transient until 30s → fail.
- `secret_lifecycle_test.py` — `make_deploy_id` produces UTC `YYYYMMDDTHHMMSSZ`; `timestamped_secret_name` / `parse_timestamped_secret_name` round-trip; GC keeps exactly the last 10 even when input is shuffled; GC tolerates non-matching names in the list (skips them).
- `migrations_test.py` — table is created on first call; backfill records existing files only when table is freshly created; pending-detection lists only on-disk files not yet in the table; apply records the row after the SQL runs (not before); apply rolls back the row insert if the SQL fails.
- `primitives_test.py` (additions) — `DevEnvName` enforces 40-char max; existing dev- prefix enforcement still holds.

### Integration tests (`test_*.py`, no marker, runs in default offload)

- `test_deploy_and_recover.py` — in-memory simulator providers for Modal (tracks deployed app versions + secrets per env), Neon (tracks restore-points + branch state), Vault (tracks readable keys), SuperTokens (tracks app existence). Each simulator exposes a `fail_next_call_to(provider_method)` knob.
  - **Happy path**: full deploy succeeds; recover-target file deleted; cleanup keeps the last 10 secrets.
  - **Failure during secret push**: deploy aborts; recover-target file present; `minds env recover` runs all reversal steps; final state matches captured target.
  - **Failure during migration**: same, plus Neon restore-point creation and restore are exercised.
  - **Failure during `modal deploy`**: same, plus `modal app rollback` is exercised (with a non-null captured version).
  - **Failure during health check**: same, full reversal runs.
  - **First-ever deploy failure**: `app_versions_to_restore` is `{rsc-dev: null, llm-dev: null}`; recover logs the warning, skips Modal rollback, still runs Neon restore + secret cleanup.
  - **Recover re-runnable**: simulate a failure during recover (e.g. Modal API down); re-run recover, assert it converges.

### Acceptance tests (`@pytest.mark.acceptance`)

**Defer for this PR.** Per user instruction, no new acceptance tests are added during this implementation. Existing acceptance tests (dev-tier deploy + destroy roundtrip) must continue to pass — the new code is a strict superset behavior-wise for those flows.

### Release tests (`@pytest.mark.release`)

**Defer for this PR.** No new release tests.

### Manual verification (done after each phase before considering it complete)

1. **Phase 1 verification**: `minds env deploy dev-<user>-1` succeeds with no Modal URL truncation; assert URLs in `~/.minds-dev-<user>-1/client.toml` match the up-front-computed URLs (no fixup phase ran).
2. **Phase 2 verification**: same deploy + destroy cycle still works under the unified `deploy_env`; the in-flight logging should mention `[lifecycle].creates_resources=true` etc.
3. **Phase 3 verification**: `modal secret list --env=dev-<user>-1` shows `<svc>-dev-<timestamp>` entries; after 11 deploys, only the last 10 remain.
4. **Phase 4 verification**: `psql $NEON_HOST_POOL_DSN -c "select * from schema_migrations"` shows backfilled rows on first new-runner deploy; subsequent deploys treat them as applied.
5. **Phase 5 verification**: `kill -9` the deploy mid-`modal deploy` (or inject a fault); confirm `.minds-deploy-recover-target.json` exists; `minds env activate` refuses with a clear message; `minds env recover` succeeds; subsequent `minds env deploy` runs cleanly. Re-run `minds env recover` with no file present → clean "nothing to recover" exit.
6. **Phase 6 verification**: deploy an intentionally-broken app version (e.g. a `raise` at module load); confirm health check fails fast with a definitive error; confirm the deploy exits non-zero with the recover guidance.
7. **Modal rollback env-var preservation smoke**: deploy → re-deploy with a different `MINDS_DEPLOY_ID` → `modal app rollback rsc-dev v<prior>` → `modal app describe rsc-dev` → confirm `MINDS_DEPLOY_ID` is back to the prior value. **This is the critical assumption underpinning the rollback design — if Modal does not preserve env vars across rollback, escalate before continuing Phase 5+.**

### Edge cases to cover in tests

- Deploy ID equality across two deploys within the same UTC second (low-likelihood, but worth asserting: a deploy that detects a collision should add a `-1` / `-2` suffix or sleep + retry — TBD; see Open Questions).
- A `lifecycle.modal_env_strategy=SHARED` config with `deploy_config.modal_env="main"` and an in-flight per-env-named recover-target (operator switched activated envs): recover refuses outright since file existence is checked first.
- Stale recover-target where the captured `neon_project_id` no longer exists: Neon restore call fails with 404; recover surfaces the error; operator must manually delete the file.
- An operator with `MINDS_ROOT_NAME=devminds` in their parent shell (noted in the session-context as a real-world footgun): preflight should detect the mismatch and fail before doing anything else.

## Open Questions

- **Neon API specifics for named restore-points.** Neon's "instant restore" feature is documented but the exact endpoint shape (URL, request body, retention) needs to be confirmed against current Neon docs during implementation. If Neon doesn't expose a named-restore-point API distinct from the timestamp-based one, fall back to capturing the timestamp in the recover-target file and calling `restore_to_timestamp` instead. Either way, the recover-target schema stays compatible (one optional `neon_restore_point_timestamp` field added if needed).
- **Does `modal app rollback` actually preserve env vars?** The Phase 5 smoke test verifies this. If it doesn't, the design pivots to baking `MINDS_DEPLOY_ID` into the deployed Docker image at build time (option 3b from the Q&A). That's a bigger change — confirm before Phase 5.
- **Modal API for `modal app history --json`** — does it return the version id in the format `modal app rollback` accepts? Both ends need to agree; if the history output uses a different id shape, an extra translation step is needed.
- **Deploy-id collisions within the same UTC second** — vanishingly unlikely in practice (deploys take minutes) but possible. Append `-N` suffix on collision, or sleep 1s and retry? Default to "fail loudly with a clear message" until we see one in the wild.
- **Where exactly do `Providers` / `ProviderCredentials` live after `provisioning.py` is deleted?** Phase 2 picks a target (likely a new `apps/minds/imbue/minds/envs/types.py` or moved into `deploy.py`); final placement to be decided during implementation based on which downstream modules need them.
- **Cleanup of orphan Neon restore-points** — we say "ages out with PITR window automatically." Need to confirm Neon's PITR retention applies to named restore-points specifically and that they don't accumulate indefinitely. If they DO accumulate, add a GC step (keep last 10, mirroring secret GC).
- **The `vault_path_prefix` / `Vault` adapter on `Providers`** — preflight wants to read individual Vault entries cheaply. Today the `read_per_env_secret_values` helper does both read + merge; preflight probably wants just the bare read. Add a `read_vault_value` method or reuse the existing one with an empty overrides dict?
- **Concurrent-deploy race** — between two operators both checking "file doesn't exist" and both writing it. Use `O_EXCL` on the tempfile + rename? `flock` on a separate lock file? File-existence check is a TOCTOU race today; if we care, add `flock`.
- **Health-check endpoint expectations on shared tiers** — `<connector_url>/generation` needs the generation id (only present when `tracks_generation=true`). On dev (`tracks_generation=false`), what's the canonical "I'm alive" endpoint? Hit `/docs` (FastAPI auto-doc, always 200 when the app is up) as a fallback for `tracks_generation=false` tiers? Or always-hit `/generation` and accept "generation not configured" as a 200-equivalent? Decide during Phase 6.
- **README / docs scope** — beyond the immediate-vicinity callouts in Phase 1 and the changelog entry in Phase 7, is there a broader documentation rewrite needed (deploy README, recovery runbook, etc.)? Likely yes; scope out separately after the code lands.
