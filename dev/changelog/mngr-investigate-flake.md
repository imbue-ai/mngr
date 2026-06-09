Fixed the `test-docker-electron` CI job, which had started failing on every
branch regardless of its diff. The `test_create_local_docker_workspace_via_electron`
e2e test clones forever-claude-template `main` at runtime, and `mngr create`
now honors FCT's `[providers.docker] docker_runtime = "runsc"` (gVisor)
hardening. The `ubuntu-latest` runner has no `runsc` registered with its Docker
daemon, so `docker run --runtime runsc` failed with "unknown or invalid runtime
name: runsc" (exit 125), the agent page never loaded, and the test timed out
after 10 minutes. The job now sets `MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME=runc`
to force the stock runtime, which is the env-var escape hatch the docker provider
config documents for environments where gVisor is unavailable.
