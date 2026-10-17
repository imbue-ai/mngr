`mngr create --format json` reports `published_endpoints`: the host-side endpoints of the create's `--publish` declarations (`specs/published-ports/spec.md`), an empty list when none were declared, so pool-bake tooling can record them on the pool row without a second `mngr list` round-trip.

`mngr create` gains `--publish <port>[/<protocol>]` (repeatable; `tcp` or `udp`, default `tcp`), the create-template key `publish` with the usual `publish__extend` stacking, and the published-ports contract providers implement (`specs/published-ports/spec.md`).
A declaration names the guest port a new host's workload listens on; the provider publishes it and records where it is reachable.

`mngr list --format json` carries `host.published_endpoints`, a list of `{guest_port, protocol, host, host_port}` objects beside `host.ssh` (filter with `size(host.published_endpoints) > 0`), for running and stopped hosts alike: the endpoints live in the host's certified record, together with a note of whether the provider re-picks the host ports on every start.
The discovery stream's `HOST_SSH_INFO` event carries the same `published_endpoints` list (additive; empty when none), from `mngr list`, the per-provider poller and the between-poll host re-reads, so a consumer following discovery learns about re-picked ports without polling.

The `local` provider records identity endpoints (the declared port on `127.0.0.1`), merged across creates since every agent shares the one local host; `--publish` on any other existing host is refused, since ports are declared at create only.
A malformed or duplicated declaration is a user-input error before any provider is called.
