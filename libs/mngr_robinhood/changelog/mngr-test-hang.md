Best-effort agent teardown (`stop_agent` / `destroy_agent`) and the SDK
restart-with-resume flow now also swallow the `CleanupFailedGroup` that `Host.stop_agents` /
`Host.destroy_agent` raise when cleanup leaves a resource behind, matching the existing
intent of logging and continuing rather than letting a teardown failure abort the run (or,
for restart, abort the relaunch).
