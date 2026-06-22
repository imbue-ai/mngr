Fixed the GIT tutorial e2e test `test_exec_force_commit` so it runs reliably: added a `@pytest.mark.timeout(120)` override (the default 10s pytest timeout was tripping during agent creation) and removed the spurious `@pytest.mark.rsync` mark (the test creates a purely-local command agent, which never invokes rsync).

Added an unhappy-path companion test `test_exec_force_commit_nothing_to_commit` covering the same tutorial command: it verifies that `mngr exec` surfaces a non-zero exit code when the forced `git commit` finds nothing to commit, and that no spurious commit lands.
