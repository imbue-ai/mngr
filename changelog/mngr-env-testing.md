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
- Added a `secrets/minds/<tier>/ovh` Vault template (AK / AS / CK) and
  documented the manual provisioning step in
  `apps/minds/docs/vault-setup.md` and
  `apps/minds/docs/host-pool-setup.md`.
