Fixed a data-loss bug in volume garbage collection and made discovery fail
loudly (instead of silently skipping) when a provider's backend is unreachable.

The data-loss bug: when the Docker daemon became briefly unavailable during a
`mngr` operation that runs GC (e.g. a Docker daemon restart), the Docker
provider's `discover_hosts` swallowed the failure and returned an empty host
list. GC then treated every volume as orphaned and deleted it -- wiping the
per-host data of still-live hosts, so their containers could no longer be
restarted. The Docker provider's `discover_hosts` now raises
`ProviderUnavailableError` when the daemon is unreachable instead of returning
`[]`, so an unreachable daemon can no longer be mistaken for "this provider has
zero hosts". GC skips an unavailable provider at its own boundary (it must not
delete volumes it cannot verify).

"Unreachable" is judged by the transport, not by the exception base class: a
dropped connection or timeout (including the daemon disappearing mid-operation,
which surfaces as a raw `requests` connection error rather than a
`DockerException`) maps to `ProviderUnavailableError`, while a
`docker.errors.APIError` -- meaning the daemon was reached and answered with an
error -- propagates as a real fault. This keeps a healthy-but-erroring daemon
from being silently treated as offline (and its provider wrongly skipped by GC).

Discovery no longer hides an unreachable provider. Previously, multi-provider
discovery silently skipped a provider whose backend was down. That meant a
command could quietly do a partial job -- e.g. `mngr message my-agent`, intended
to reach every instance of `my-agent`, could miss an instance on a down provider
without telling you. Now `discover_hosts_and_agents` propagates
`ProviderUnavailableError`, so commands that scan every provider (`message`,
`limit`, `snapshot`, `create`) fail loudly rather than silently omit agents on
the unreachable provider.

Targeted commands now scope discovery so an *unrelated* down provider can't fail
them. `mngr rsync`, `mngr git push`/`pull`, and `mngr event <host>` now resolve
only the provider(s) that could actually hold the target -- via the `.PROVIDER`
qualifier and/or the agent name (resolved through the discovery event stream) --
instead of blindly scanning every provider. So `mngr rsync ./x agent@host.local`
keeps working when an unrelated Docker daemon is down (Docker is never queried),
while a command whose target really is on the down provider fails with a clear
"provider is not available" error.
