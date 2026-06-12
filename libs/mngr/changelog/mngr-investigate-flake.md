The docker provider now raises a typed, actionable `DockerRuntimeNotRegisteredError`
when the configured `docker_runtime` (e.g. `runsc` for gVisor) is not registered
with the Docker daemon, instead of letting Docker's raw exit-125 `ProcessError`
propagate. The old behavior surfaced the entire `docker run` command line with the
real cause ("unknown or invalid runtime name: runsc") buried inside it and no
guidance. The new error renders as a clean message naming the runtime and provider,
with `user_help_text` pointing at the fix (install the runtime, or set
`docker_runtime=runc` via `mngr config set` / the
`MNGR__PROVIDERS__<NAME>__DOCKER_RUNTIME` env var). Because it is an `MngrError`
subclass, `mngr create --format jsonl` now emits `error_class:
"DockerRuntimeNotRegisteredError"` so callers can branch on the type.
