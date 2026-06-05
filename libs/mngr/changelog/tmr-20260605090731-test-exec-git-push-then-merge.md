Fixed the `test_exec_git_push_then_merge` e2e release test. It was marked
`@pytest.mark.modal` even though its body only creates a local command agent and
runs local git operations (`mngr exec`, `git fetch`, `git merge`), so it never
invokes Modal -- the resource guard failed the otherwise-passing test with "never
invoked modal". Removed the spurious mark. The test now also verifies the actual
behavior: that the agent-side `git push` fails because no `origin` remote is
configured (proving `mngr exec` forwards the command to the agent host and
surfaces its non-zero exit code) and that the local `git fetch && git merge` chain
succeeds as an up-to-date no-op.
