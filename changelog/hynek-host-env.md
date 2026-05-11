## mngr-latchkey: per-host permissions storage

Reorganized how minds keys latchkey permissions and how they flow into
the agent's host:

- Latchkey env vars (`LATCHKEY_GATEWAY[_PASSWORD,_PERMISSIONS_OVERRIDE,_DISABLE_COUNTING]`)
  are now injected via `mngr create --host-env` instead of `--env`, so
  they land on the agent's host environment (where the FCT-template
  latchkey wiring reads them) rather than only the agent process.
- Permissions are stored per host, keyed by host name, under
  `<plugin_data_dir>/hosts/<host_name>/latchkey_permissions.json` --
  replacing the previous opaque `<plugin_data_dir>/permissions/<uuid>.json`
  symlink-after-create indirection. Since minds knows the host name
  up-front (`{agent_name}-host`), the JWT can be minted directly for
  the canonical host path before `mngr create` runs.
- Each host directory also holds a `host-id` file recording the
  canonical `HostId` reported by `mngr create`. When an agent is
  created, `finalize_agent_permissions` cross-checks the recorded id
  against the new one; a mismatch (or no recorded id) means the host
  with that name has been recreated, so the permissions file is
  cleared to prevent the fresh host from inheriting stale grants from
  a previous tenant with the same name.
- API changes in `imbue.mngr_latchkey`:
  - `prepare_agent_latchkey(latchkey, host_name, *, is_tunneled=...)`
    now takes the `HostName` explicitly and returns an
    `AgentLatchkeySetup` with only the `env` field (the
    `opaque_permissions_path` attribute is gone).
  - `finalize_agent_permissions(latchkey, host_name, host_id)` now
    takes `host_name` + `HostId` and reconciles the `host-id` file;
    it no longer takes an opaque path.
  - New store helpers: `permissions_path_for_host`, `host_id_path_for_host`,
    `host_data_dir`, `read_stored_host_id`, `write_stored_host_id`.
  - Removed store helpers: `permissions_path_for_agent`,
    `opaque_permissions_dir`, `new_opaque_permissions_path`,
    `link_opaque_permissions_to_agent`.
- Minds-side: `_make_host_name` is now public as
  `imbue.minds.desktop_client.agent_creator.make_host_name_for_agent`
  so the permission grant handler can derive a host name from the
  resolver's `AgentDisplayInfo.agent_name`. The grant handler now
  takes `host_name: HostName` alongside `agent_id` and writes /
  reads `latchkey_permissions.json` under the per-host directory.
