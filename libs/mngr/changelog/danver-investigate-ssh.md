Fixed a crash that made statically-configured SSH hosts unusable, and added documentation for the SSH provider.

- Static `[providers.<pool>.hosts.<host>]` tables defined in `settings.toml` previously crashed every
  host-enumerating command (`mngr list`, `mngr connect`, `mngr create <agent>@<host>.<pool>`, ...) with
  `AttributeError: 'dict' object has no attribute 'key_file'`. Provider configs are built with
  `model_construct` (to keep unset top-level fields `None` for config-layer merging), which does not
  coerce nested values, so each host entry stayed a raw dict instead of an `SSHHostConfig` and blew up
  as soon as the backend touched it. The config loader now coerces nested pydantic-model fields (the
  only one today being the SSH provider's `hosts` map) after `model_construct`, so static SSH hosts load
  and resolve correctly. A malformed host entry now produces a clear `providers.<pool>.hosts` config
  error instead of a late crash.
- Added an [SSH provider documentation page](../docs/core_plugins/providers/ssh.md) covering host
  configuration (`address`/`port`/`user`/`key_file`/`known_hosts_file`), the dynamic-hosts file, the
  `NAME@HOST.PROVIDER` form for running an agent on a configured host, and the provider's limitations
  (no host creation/snapshots/tags). Registered the `ssh` backend in the provider concepts doc.
