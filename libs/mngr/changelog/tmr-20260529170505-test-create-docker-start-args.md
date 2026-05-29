Fixed the `test_create_docker_start_args` e2e tutorial test so its `mngr create`
invocation passes an explicit agent type. The tutorial block assumes a configured
default agent type (claude), which the isolated test profile does not set, so the
test now uses `--type command -- sleep ...` as a stand-in (matching the convention
used across the other e2e tutorial tests). The tutorial block itself is unchanged.
