Simplified exception handlers now that `HostError` is a `MngrError` subclass: the redundant
`HostError` entry in the `except (MngrError, HostError, ...)` guards in launching and the CLI
has been removed. `AgentError` (still a `BaseMngrError`, not a `MngrError`) is retained. No
behavior change.
