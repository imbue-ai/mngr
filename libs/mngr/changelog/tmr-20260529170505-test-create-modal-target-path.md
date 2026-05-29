Fixed the `test_create_modal_target_path` e2e tutorial test, which exercises the
`mngr create my-task@.modal:/workspace` address syntax for specifying the remote
work-directory mount path. The test now supplies an explicit `--type command`
agent (the isolated e2e environment configures no default agent type) and
verifies that the agent's work directory is actually mounted at the requested
target path (`/workspace`).
