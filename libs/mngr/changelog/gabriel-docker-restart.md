Fixed a data-loss bug in volume garbage collection. When the Docker daemon
became briefly unavailable during a `mngr` operation that runs GC (e.g. a Docker
daemon restart), the Docker provider's `discover_hosts` swallowed the failure and
returned an empty host list. GC's `_discover_hosts_for_gc` only skips a provider
whose discovery *raises*, so it recorded the provider with zero hosts; `gc_volumes`
then treated every volume as orphaned and deleted it -- wiping the per-host
`host_dir` data of still-live hosts (their host records survived, but their volume
directories did not, so the containers could no longer be restarted).

The Docker provider's `discover_hosts` now raises `ProviderUnavailableError` when
the daemon is unreachable instead of returning `[]`. Like the Modal and Imbue
Cloud providers, it now propagates a discovery failure rather than swallowing it
into an empty list, so an unreachable daemon can no longer be mistaken for "this
provider has zero hosts". Discovery failures are skipped per-provider by the
existing handlers (GC skips the provider; `mngr list` reports it as unavailable).

Multi-provider discovery (`discover_hosts_and_agents`, used by `mngr rsync`,
`git`, `find`, `message`, ...) now also skips an unreachable provider and
continues with the ones that are available, per the documented
`ProviderUnavailableError` contract. Previously a single offline backend would
abort the whole command; now, for example, `mngr rsync <local-agent>` still works
when an unrelated Docker daemon is down. Genuine (non-availability) discovery
errors still surface as before. As a result, `mngr` commands that scan all providers now surface a clear
"provider unavailable" error when Docker is off, rather than silently omitting
Docker agents.
