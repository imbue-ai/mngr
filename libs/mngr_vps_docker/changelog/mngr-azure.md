## Stopped containers no longer misreport as CRASHED / vanish from `mngr conn`

- Fixed: a VPS-Docker host whose container is stopped while the VPS itself is still reachable (the idle-watcher shutdown, a manual `mngr stop`, or a VPS reboot) is now reported as `STOPPED` and stays visible to `mngr conn` / `mngr start`, instead of being misreported as `CRASHED` and filtered out of the connect path entirely (which produced a confusing "Could not find agent" even though the agent had stopped cleanly and its data was intact).

  Discovery now distinguishes a reachable-but-container-down host (clean stop) from an unreachable VPS (the genuine down/crash case). The latter is unchanged: it stays hidden from `include_destroyed=False` callers and surfaces as `CRASHED` in `mngr list`. This affects all VPS-Docker providers (aws/gcp/azure/vultr/ovh).
