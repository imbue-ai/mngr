`stop_agent_on_host` now also tolerates the `CleanupFailedGroup` that `Host.stop_agents`
raises when cleanup leaves a resource behind, so a best-effort stop in a `finally` logs and
continues instead of masking the real result.
