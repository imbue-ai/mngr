Fixed the `test_exec_git_log` e2e tutorial test (WORKING WITH GIT section). The
shared `_create_my_task` helper now creates the agent on Modal (`--provider
modal`) with a remote-sized timeout, so the test exercises the real remote path
and satisfies its `@pytest.mark.modal` / `@pytest.mark.rsync` marks. Removed the
erroneous `@pytest.mark.tmux` mark (tmux runs on the remote host, not locally,
for a Modal agent) and added a per-test timeout. The test now also asserts that
the agent's `git log` output contains the seed commit, verifying actual behavior
rather than only the exit code.
