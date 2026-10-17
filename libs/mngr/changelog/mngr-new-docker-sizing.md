The docker provider now records, reports, and can change a container's CPU and memory caps.

- New `[providers.docker]` settings `default_cpus` and `default_memory` cap every new container (`docker run --cpus` / `--memory`, with `--memory-swap` set equal to the memory cap so the container cannot swap). A `--cpus` or `--memory` in `default_start_args` or a caller's `-s` flag still wins, since docker keeps the last spelling of a repeated flag. A `default_cpus` above the daemon's CPU count is clamped with a warning instead of failing the create.

- `mngr list` reports a docker host's real size (`host.resource.cpu.count` / `memory_gb`) instead of the old 1 CPU / 1 GB placeholder: the caps recorded in its `docker run` arguments, with any uncapped dimension filled in from the daemon machine's totals. Disk stays unreported (the host volume has no quota of its own).

- Stopped hosts now list their size too: `get_provider_resources` moved from `OnlineHostInterface` to `HostInterface`, offline hosts answer it from the provider's records, and the default listing path (docker, lima) fills `resource` for offline hosts. The unreachable-host fallback tolerates a provider whose record read fails (size unknown, with a warning) rather than failing the listing.

- New `mngr docker resize HOST [--cpus N] [--memory SIZE]` rewrites the caps recorded for a docker host and applies them to its container with `docker update`, running or stopped; a value docker refuses is reported and leaves the record untouched. `mngr start` and a snapshot restore re-apply the recorded size, so it survives out-of-band `docker start`s and daemon restarts.
