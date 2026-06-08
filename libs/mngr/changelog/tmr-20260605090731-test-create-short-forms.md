Fixed the `test_create_short_forms` e2e tutorial test (BASIC CREATION). It now
carries an explicit `@pytest.mark.timeout(120)` because it issues two `mngr
create` commands, whose combined function-body time exceeds the global 10s
pytest-timeout default. Removed its `@pytest.mark.modal` mark: the test only
creates local (`--type command`) agents and runs `mngr list`, which reaches
Modal exclusively via the in-process gRPC SDK inside the spawned `mngr`
subprocess -- a path the resource guard cannot track -- so the mark tripped the
guard's NEVER_INVOKED check. No user-facing behavior changes.
