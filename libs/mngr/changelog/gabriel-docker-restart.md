Fixed a data-loss bug in volume garbage collection. When the Docker daemon
became briefly unavailable during a `mngr` operation that runs GC (e.g. a Docker
daemon restart), the Docker provider's `discover_hosts` swallowed the failure and
returned an empty host list. GC's `_discover_hosts_for_gc` only skips a provider
whose discovery *raises*, so it recorded the provider with zero hosts; `gc_volumes`
then treated every volume as orphaned and deleted it -- wiping the per-host
`host_dir` data of still-live hosts (their host records survived, but their volume
directories did not, so the containers could no longer be restarted).

The Docker provider's `discover_hosts` now raises `ProviderUnavailableError` when
the daemon is unreachable instead of returning `[]`, matching the Modal and Imbue
Cloud providers. Discovery failures are now skipped per-provider by the same
existing handlers (GC skips the provider; `mngr list` reports it as unavailable),
so an unreachable daemon can no longer be mistaken for "this provider has zero
hosts". As a result, `mngr` commands that scan all providers now surface a clear
"provider unavailable" error when Docker is off, rather than silently omitting
Docker agents.
