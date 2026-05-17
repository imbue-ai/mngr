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
- Renamed `vps_ip` -> `vps_address` end-to-end: API models
  (`LeaseResult`, `LeasedHostInfo`, `LeaseHostResponse`), all Python
  call sites, AND the `pool_hosts.vps_ip` DB column. Migration ships
  as `apps/remote_service_connector/migrations/003_vps_address.sql`
  (idempotent rename). The field can hold a public IPv4 or a DNS
  hostname (e.g. OVH's `vps-eec8860b.vps.ovh.us`).
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
