Fix the `test_event_head` tutorial e2e release test so it reflects how local agents are actually created and reads events reliably.

The test (and its shared `_create_my_task` helper) carried `@pytest.mark.rsync`, but a local agent created from a git repo on the same host uses the git-worktree transfer mode, which never invokes rsync. The resource guard correctly flagged the mark as never invoked. Removed the `rsync` mark and corrected the accompanying comments to describe git-worktree creation.

The two `mngr event` reads also relied on the 30s default e2e timeout, which a cold mngr invocation right after agent creation can exceed; they now pass `timeout=60.0`, matching the headroom already used by `test_event_default` for the same command.
