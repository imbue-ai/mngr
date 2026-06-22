Fixed the tutorial event e2e tests (`test_event.py`): a local command agent created in a git repo materializes its work dir via a git worktree, not rsync, so the incorrect `@pytest.mark.rsync` mark (which tripped the resource-guard "marked but never invoked rsync" check) was removed from all event tests and the helper comment was corrected.

Strengthened `test_event_tail` to verify `mngr event --tail N` returns the trailing suffix of the full event stream, mirroring the existing prefix check in `test_event_head`.
