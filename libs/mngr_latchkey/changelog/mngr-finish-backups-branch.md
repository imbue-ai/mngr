Fixed a startup race in which an early `LatchkeyForwardSupervisor.bounce()` could silently kill a freshly-spawned `mngr latchkey forward` supervisor: the forward publishes its pid record well before it installs its SIGHUP handler, so a bounce landing in that window terminated it via the signal's default disposition (observed in practice from the minds discovery-health watchdog sampling mid-startup).

- `mngr latchkey forward` now ignores SIGHUP for its whole startup window (from process start until the real bounce handler is installed), so an early bounce is dropped instead of fatal.

- `LatchkeyForwardSupervisor.bounce()` now skips the SIGHUP entirely while the forward's record carries no gateway port (i.e. startup has not finished): the `mngr observe` child it would bounce does not exist yet, and startup reads the current provider state anyway.
