Fixed the `test_create_docker_volume_start_arg` e2e tutorial test, which was
failing because `mngr create` requires an agent type and the test supplied
none. The test now uses the standard `--type command -- sleep <N>` stand-in
(matching the other tutorial create tests) and verifies that the `-v` start arg
actually bind-mounts the host directory into the container.
