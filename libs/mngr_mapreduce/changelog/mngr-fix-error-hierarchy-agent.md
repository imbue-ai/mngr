Simplified exception handlers now that `AgentError` is a `MngrError` subclass: the redundant
`AgentError` entry in the `except (MngrError, AgentError, ...)` guards in launching and the CLI
has been removed. No behavior change -- agent errors are still caught and handled the same way.
