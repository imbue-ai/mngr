Simplified exception handlers now that `HostError`/`HostConnectionError` are `MngrError`
subclasses: the redundant `except (HostConnectionError, MngrError)` guards in the VPS Docker
instance are now just `except MngrError`. No behavior change -- host connection errors are
still caught and handled the same way.
