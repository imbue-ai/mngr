Preserved connection-error handling in the Modal provider's optimized listing now that
`HostConnectionError` is a `MngrError` subclass. `get_host_and_agent_details` now re-raises
`HostConnectionError` from the inner SSH-collection guard so it still reaches the outer
handler that clears the per-host connection cache and falls back to the default listing,
instead of being swallowed by the inner `except MngrError`.
