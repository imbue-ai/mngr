Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`); the entries `mngr-per-client-host-keys.md` and `mngr-fix-provider-storage.md` in this directory belong to the same PR.

- The shared provider release trips keep main's recorded-size assertion and the branch's per-host VPS-root key assertions; the regenerated `imbue_cloud` command docs cover main's newer subcommands and the `sync documents` subgroup.

- `imbue.mngr.providers.host_access_manifest` adds `HostAccessManifest`: the per-host access material a provider keeps under an instance dir (key dir, per-host dir scheme, the key files, the agent key, the pin stores and the per-host `known_hosts` link), with the path resolvers a consumer carrying a host's access to another machine needs. Each provider publishes one; the minds workspace record reads the layouts from them instead of restating five of its own.

- `HostAccessManifest` says it describes the provider's default placement: its `agent_key_file_name` and `known_hosts_link_store_name` descriptions name that placement, since the vps providers' bare placement links its per-host `known_hosts` at a different store and is not published.
