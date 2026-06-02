Simplified an exception handler now that `HostError`/`HostConnectionError`/`HostNotFoundError`
are all `MngrError` subclasses: the redundant `except (HostConnectionError, HostNotFoundError,
MngrError)` guard is now just `except MngrError`. No behavior change.
