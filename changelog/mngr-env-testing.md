Minds dev-environment fixes:

- Documentation now leads with the `dev-<your-user>` naming convention
  (so dev env roots come out as `~/.minds-dev-<your-user>/` and
  `MINDS_ROOT_NAME=minds-dev-<your-user>`, tier-first).
- `minds env activate` now exports `MODAL_PROFILE` derived from the
  activated tier's committed `modal_workspace`. Every subsequent
  `modal` CLI shellout (deploy, secret create, environment create) is
  pinned to the right workspace regardless of which profile is marked
  `active = true` in `~/.modal.toml`. Prerequisite: the operator must
  have a matching profile in `~/.modal.toml` for each tier
  (`modal token set --profile <workspace>` once per tier). Skipped
  when the tier's `modal_workspace` is still the literal `CHANGE_ME`
  placeholder.
- Renamed `LeaseResult.vps_ip` / `LeasedHostInfo.vps_ip` /
  `LeaseHostResponse.vps_ip` to `vps_address` (the field can be a
  public IPv4 *or* a DNS hostname like the OVH serviceName
  `vps-eec8860b.vps.ovh.us`). The underlying `pool_hosts.vps_ip` DB
  column is unchanged.
- `min_containers` for the deployed `remote-service-connector-<tier>`
  and `litellm-proxy-<tier>` Modal apps is now configurable: defaults
  to `0` for dev and `1` for staging / production, overridable at
  `modal deploy` time via `MINDS_MIN_CONTAINERS=<n>`.
- Added a `secrets/minds/<tier>/ovh` Vault template (AK / AS / CK) and
  documented the manual provisioning step in
  `apps/minds/docs/vault-setup.md` and
  `apps/minds/docs/host-pool-setup.md`.
