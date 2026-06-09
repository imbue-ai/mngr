Fixed the SSH provider silently disabling strict host-key checking for statically-configured hosts.

- A host defined under `[providers.<pool>.hosts.<host>]` that set **both** `key_file` and
  `known_hosts_file` lost its `known_hosts_file` whenever the backend expanded the `key_file` path.
  `SSHProviderBackend.build_provider_instance` rebuilt the `SSHHostConfig` by re-listing only
  `address`/`port`/`user`/`key_file`, so `known_hosts_file` silently became `None` and strict
  host-key checking was turned off for that host. The backend now updates only the `key_file`
  field, preserving `known_hosts_file` (and any future fields), matching the dynamic-hosts path
  that already did this correctly.
