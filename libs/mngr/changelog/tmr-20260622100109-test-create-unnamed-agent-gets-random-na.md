Fixed the `test_create_unnamed_agent_gets_random_name` release test (in `e2e/tutorial/test_create_basic.py`):

- Scoped its verification listing to `mngr list --provider local` (the agent is a local `--type command` agent). An unscoped `mngr list` runs full provider discovery and, by design, aborts with a non-zero exit when any enabled-but-unreachable backend is present. The bundled cloud backends (aws/gcp/azure/vultr) and docker are all discoverable yet unconfigured/unavailable in the isolated e2e fixture, so the unscoped listing aborted before the agent could be inspected.

- Removed the superfluous `@pytest.mark.rsync` mark. The default worktree create runs against the fixture's clean git repo, so `_transfer_extra_files` finds nothing to transfer and never invokes rsync, which tripped the resource guard's "marked but never invoked" check once the listing was fixed.
