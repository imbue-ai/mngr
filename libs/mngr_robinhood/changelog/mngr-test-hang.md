Best-effort agent teardown (`stop_agent` / `destroy_agent`) now also swallows the
`CleanupFailedGroup` that `Host.stop_agents` / `Host.destroy_agent` raise when cleanup
leaves a resource behind, matching the existing intent of logging and continuing rather
than letting a teardown failure abort the run.
