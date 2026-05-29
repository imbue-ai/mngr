Fixed the `test_create_modal_cpu_memory_gpu` release test, which had been failing
since the `mngr create` agent-type default was moved into user config (the
isolated e2e profile has no default, so the command was rejected with "No agent
type provided"). The test now passes `--type claude` explicitly, matching the
agent type these commands previously assumed by default. Also strengthened the
test to verify the build-arg'd Modal host actually booted and is reachable via
`mngr exec`, rather than only asserting the create command exited cleanly.
