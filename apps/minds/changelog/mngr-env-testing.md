Batch of `minds env deploy` / connector follow-ups from the F-numbered
findings in `MANUAL_DEPLOY_FINDINGS.md`:

- ``minds env deploy``'s post-deploy health check now polls the connector's
  new ``GET /health/liveness`` route instead of ``/docs`` (smaller, faster,
  symmetric with the LiteLLM proxy's existing liveness probe). The
  per-attempt HTTP timeout bumped from 3s to 10s and the total budget
  from 30s to 60s so cold-booting Modal containers have a realistic
  chance to respond before being declared unhealthy. (F2, F3)
- ``DeployLifecycleConfig`` has a new pydantic model validator that
  rejects ``writes_local_state=true`` + ``creates_resources=false``
  at deploy.toml parse time. The combination would previously have
  AssertionError'd partway through deploy AFTER both Modal apps had
  been deployed; failing at config load is far less surprising. The
  matching asserts in ``deploy_env`` stay as defense-in-depth for
  non-CLI callers. (F18)
- ``minds env deploy`` runs ``apply_pool_hosts_migrations`` for every
  tier instead of only the dev tier. Shared tiers (staging /
  production) source the host_pool DSN from ``DATABASE_URL`` in their
  operator-managed ``secrets/minds/<tier>/neon`` Vault entry. Without
  this, a new ``.sql`` migration shipped via PR would apply to dev
  envs immediately but never to staging / production until the
  operator ran psql manually -- and the two schemas would diverge.
  (F17)
- ``minds env destroy`` proceeds with cloud-side cleanup even when the
  local env root has already been removed by hand. The cloud-side
  resources are keyed off the env *name*, not the local directory, so
  an operator who ``rm -rf``'d ``~/.minds-<env>/`` can still re-run
  destroy by name to clean up Modal apps / Neon / SuperTokens /
  Cloudflare tunnels / OVH instances. ``destroy_env`` no longer
  raises ``DevEnvNotFoundError`` for missing-root; it logs a warning
  and proceeds. Step 1 (``mngr destroy`` per agent) becomes a no-op
  since the agents directory is gone too. (F22)
- ``per_env_connector_url`` / ``per_env_litellm_proxy_url`` now take
  the ``tier`` as a keyword arg. The dev URLs stay shaped as
  ``rsc-dev`` / ``llm-dev`` so existing per-env deployments keep
  working without a redeploy, but any future ``PER_ENV`` tier other
  than dev gets the right ``rsc-<tier>`` segment automatically
  instead of silently colliding on the hardcoded ``dev`` segment.
  (F24)
- ``minds env deploy``'s ``find_monorepo_root`` check happens BEFORE
  the Vault credential read in the CLI and BEFORE
  ``make_deploy_id`` inside ``deploy_env``. Running from outside
  the monorepo now fails immediately with a clean error rather than
  reading Vault first and logging a misleading "Deploy id: ..."
  line. (F15)
- ``minds env list`` resolves the reserved tiers' (``production`` /
  ``staging``) client.toml to the committed in-repo
  ``apps/minds/imbue/minds/config/envs/<tier>/client.toml`` instead
  of showing ``(no client.toml)``. The ``DevEnvSummary`` gains a
  ``client_config_source: "env_root" | "in_repo" | None`` field so
  machine consumers can distinguish "per-env file" from "in-repo
  file" from "unprovisioned." Human-format output now reads
  ``<path>  (in-repo, committed)`` for reserved tiers and
  ``(no client.toml -- run `minds env deploy`)`` for unprovisioned
  dev envs. (F11)
- The ``litellm-connector`` Modal Secret no longer appears in
  ``[secrets].services`` -- it was never vault-backed (no
  ``secrets/minds/<tier>/litellm-connector`` Vault entry exists),
  and the carve-out (``_DERIVED_ONLY_SECRET_SERVICES``) that
  suppressed a misleading per-deploy warning was a code smell. The
  deploy now pushes the secret as a separate code-driven step at
  the end of the secret-push loop; ``_DERIVED_ONLY_SECRET_SERVICES``
  is deleted. ``[secrets].services`` in every tier's deploy.toml
  becomes a truthful "vault-backed only" list. The post-deploy GC
  picks up ``litellm-connector-<tier>-<deploy_id>`` secrets via the
  same suffix-match pattern, so no GC bookkeeping changes. (F25)
- Recover changes: when the captured pre-deploy app version is
  ``None`` (a first-ever deploy of this env / tier), ``minds env
  recover`` now ``modal app stop``s the deployed app instead of
  leaving it running -- otherwise the just-deleted Modal Secrets
  would leave the app 500'ing on every request. (F19)
- The recover-target file is per-env: each in-flight deploy gets
  its own ``.minds-deploy-recover-target-<env>.json`` at the
  monorepo root, so concurrent deploys against different envs don't
  refuse each other (useful for parallel test runs). The
  environment-scoped commands (``deploy`` / ``destroy``) refuse only
  if THEIR env's file exists; the env-agnostic commands (``activate``
  / ``deactivate`` / ``list``) still refuse loud if ANY recover-
  target file exists (listing all in the error). ``deploy_env`` and
  ``recover_env`` additionally hold a per-env ``flock`` on
  ``.minds-deploy-lock-<env>.lock`` for their entire process
  lifecycle, so two concurrent invocations against the same env
  serialize at the kernel level. (F26)
- Doc / spec updates: comment on the connector's ``/generation`` env
  var now explains empty-string is the steady state for
  ``tracks_generation=false`` tiers (not a legacy artifact). Spec
  ``specs/minds-deploy-safety-overhaul/spec.md`` updated to use
  branch-based Neon-snapshot terminology (the implementation pivoted
  from the spec's original named-restore-point design because Neon's
  named-restore-point API is org-tier-gated) and to refer to
  ``/health/liveness`` on both apps. (F1, F4, F9, F20)

Minds dev-environment fixes:

- Hard-enforces the `dev-<your-user>` naming convention for dev envs:
  `DevEnvName` rejects anything that does not start with `dev-`, and
  `MINDS_ROOT_NAME_PATTERN` only accepts `minds`, `minds-staging`, or
  `minds-dev-<rest>`. Dev env roots come out tier-first as
  `~/.minds-dev-<your-user>/` and `MINDS_ROOT_NAME=minds-dev-<your-user>`.
- `minds env activate` now exports `MODAL_PROFILE` derived from the
  activated tier's committed `modal_workspace`. Every subsequent
  `modal` CLI shellout (deploy, secret create, environment create) is
  pinned to the right workspace regardless of which profile is marked
  `active = true` in `~/.modal.toml`. Prerequisite: the operator must
  have a matching profile in `~/.modal.toml` for each tier
  (`modal token set --profile <workspace>` once per tier). Skipped
  when the tier's `modal_workspace` is still the literal `CHANGE_ME`
  placeholder.
- `min_containers` for the deployed `remote-service-connector-<tier>`
  and `litellm-proxy-<tier>` Modal apps is now driven by a tier's
  committed `deploy.toml` via a new `[min_containers]` block (fields:
  `connector`, `litellm_proxy`). Defaults to 0 in the Pydantic model;
  staging / production deploy.toml ship with `1` for both. The values
  thread into `modal deploy` as `MINDS_CONNECTOR_MIN_CONTAINERS` /
  `MINDS_LITELLM_PROXY_MIN_CONTAINERS`, which the modal app modules
  read at import time.
- Per-dev-env Neon **project** (not just a database): each dev env
  now owns a brand-new Neon project named `minds-<env>` under the
  dev-tier Neon org, containing two databases (`host_pool` and
  `litellm_cost`). `minds env deploy` provisions the project and
  applies the `pool_hosts` schema (via `apps/remote_service_connector/
  migrations/*.sql`) to `host_pool` automatically. `minds env destroy`
  deletes the project outright -- atomic teardown of both DBs, roles,
  and the project's pooler endpoint.

  The deploy now overrides BOTH `neon.DATABASE_URL` and
  `litellm.DATABASE_URL` in the per-env Modal Secrets with the per-env
  project's two DSNs, so the connector and the LiteLLM proxy talk to
  the same env-isolated Neon project. The per-env `secrets.toml` on
  disk grows two fields (`NEON_HOST_POOL_DSN`, `NEON_LITELLM_DSN`,
  replacing the single `NEON_POOLED_DSN`).

  Vault `secrets/minds/<tier>/neon-admin` now expects `NEON_ORG_ID`
  (instead of `NEON_PROJECT_ID`). The token must have project-create
  scope on the dev tier's Neon org.

  `mngr imbue_cloud admin pool create` and friends now auto-resolve
  `--database-url` from the activated minds env's `NEON_HOST_POOL_DSN`
  (or `MINDS_HOST_POOL_DSN` env var), so the standard dev-env flow no
  longer requires passing the DSN explicitly. Operators outside an
  activated env still pass `--database-url` directly.

  Staging / production keep the tier-shared single-DB model unchanged.

- Added a `secrets/minds/<tier>/ovh` Vault template (AK / AS / CK) and
  documented the manual provisioning step in
  `apps/minds/docs/vault-setup.md` and
  `apps/minds/docs/host-pool-setup.md`.

- `minds env deploy` is now actually idempotent against Neon. The
  Neon REST API does not 409 on duplicate project names within an
  organization -- POSTing `/projects` with a name that's already in
  use creates a second, distinct project with the same name and a
  different id. The previous `create_neon_project` assumed Neon would
  409 (the adopt-fallback path was never reached), so every dev-tier
  re-deploy silently leaked an entire Neon project (with its own
  host_pool + litellm_cost DBs + branches + endpoints). Several
  attempts at deploying dev-josh-1 during one session today left
  four projects named `minds-dev-josh-1` in the dev org. The same
  bug would have caused `minds env destroy` to delete the wrong
  project (always the first match from the list endpoint, i.e. the
  oldest, not the live one), leaving the live project stranded.
  `create_neon_project` and `delete_neon_project` now look up by
  name first via `_find_projects_by_name`, adopt when there's
  exactly one match, raise a `NeonProviderError` with every
  matching project id + creation timestamp + a copy-pasteable
  cleanup recipe when there are several. Refusing-loud is
  intentional: silently picking one would risk destroying the wrong
  project under a real name collision (e.g. two devs using the same
  env name cross-machine). A new `_select_one_or_raise_multi_match`
  pure helper carries the decision logic; the operator-facing error
  message is unit-tested.

Minds deploy safety overhaul (spec
`specs/minds-deploy-safety-overhaul/spec.md`):

- Shorter Modal app + function names so the deployed hostname stays
  under DNS's 63-char limit for every realistic env name:
  `remote-service-connector` -> `rsc`, `fastapi_app` -> `api`,
  `litellm-proxy` -> `llm`, `litellm_app` -> `proxy`. Modal workspaces
  rename to `minds-dev` / `minds-staging` / `minds-production`. URL
  is now exactly what we compute up front, so the deploy no longer
  runs a second-pass secret push or a connector redeploy. `DevEnvName`
  enforces a 40-char max so the hostname budget always fits.

- One `minds env deploy` path for every tier, driven by a new required
  `[lifecycle]` block in each tier's `deploy.toml` (flags:
  `creates_resources`, `modal_env_strategy`, `writes_local_state`,
  `tracks_generation`). dev / staging / production all execute the
  same code now; behavior diverges only via the flag matrix.
  `deploy_dev_env` + `deploy_tier_env` collapse into `deploy_env`.
  Inline best-effort rollback machinery (`_best_effort_rollback`,
  `_ROLLBACK_TABLE`, `_rollback_*`) deleted -- replaced by
  `minds env recover` (below). Production now `tracks_generation=true`
  for parity with staging (production destroy is hard-refused so the
  generation id is effectively immutable for the tier's lifetime).

- Pool-hosts schema migrations now backed by a real
  `schema_migrations(version, applied_at)` table instead of the old
  "replay every .sql with IF NOT EXISTS guards". New
  `apps/minds/imbue/minds/envs/migrations.py` owns the runner. Legacy
  files keep their `IF NOT EXISTS` guards so a backfill against an
  already-migrated DB is a no-op + records the row; new migrations
  land WITHOUT guards (the table is the source of truth).

- Every `minds env deploy` mints a fresh `MINDS_DEPLOY_ID` (UTC
  `YYYYMMDDTHHMMSSZ`) and pushes every Modal Secret under a new name
  `<svc>-<tier>-<deploy_id>` (no overwrites). The deployed Modal apps
  read `MINDS_DEPLOY_ID` at module load and pin every
  `Secret.from_name(...)` to the matching timestamped name. Hard-fails
  at module load if `MINDS_DEPLOY_ID` is missing (no fallback to
  unsuffixed names; manual `modal deploy` outside `minds env deploy`
  is unsupported). End-of-deploy GC keeps the last 10 timestamped
  secrets per `<svc>-<tier>`; shared-tier destroy deletes all
  timestamped secrets matching the tier.

- New `minds env recover` command + recover-target file at the
  monorepo root. Every deploy captures pre-deploy Modal app versions,
  creates a Neon snapshot branch (`pre-deploy-<deploy_id>` off the
  default branch -- COW so it's near-free), and writes
  `.minds-deploy-recover-target.json` (gitignored) atomically BEFORE
  touching any external state. On a failed deploy, the operator runs
  `minds env recover`; it idempotently runs every reversal step
  (`modal app rollback`, Neon branch-restore from the snapshot with
  the pre-restore state preserved under `pre-rollback-<deploy_id>`,
  delete orphan timestamped secrets, delete the recover-target file).
  Successful deploys delete the snapshot branch (best-effort cleanup).
  Every other `minds env *` command (`activate` / `deactivate` /
  `list` / `deploy` / `destroy`) refuses to run while a recover-
  target file exists.

  Snapshot/restore works for every tier (dev creates_resources=true
  and shared creates_resources=false). Shared tiers (staging /
  production) require `NEON_PROJECT_ID` in their
  `secrets/minds/<tier>/neon-admin` Vault entry; the deploy resolves
  the default branch on demand. Without `NEON_PROJECT_ID` shared-tier
  deploys log a warning and skip the snapshot (so recover can roll
  back Modal apps but not the DB).

- Post-deploy health check: `await_apps_healthy` polls
  `<connector>/docs` and `<litellm_proxy>/health` for up to 30s each
  (sequential), with cold-boot 5xx tolerance + immediate failure on
  4xx / 5xx-with-body / wrong-shape responses. Failure surfaces as
  `HealthCheckFailedError` and goes through the same "run
  `minds env recover`" guidance as any other deploy failure.

- Each deploy also gets a `[lifecycle].tracks_generation=true` tier
  generation id minted into the litellm-connector Modal Secret (no
  change for dev / staging; production now also gets one).

Operator-visible: re-deploys after any of the above are
backwards-compatible against the existing dev-tier resources. The
shared (`staging` / `production`) tiers' `deploy.toml` files now
require a `[lifecycle]` block; operators bringing up staging /
production for the first time need to populate the existing OAuth
client IDs as before plus ensure the `[lifecycle]` block matches the
defaults documented in the committed file.
