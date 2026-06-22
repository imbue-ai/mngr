Removed the superfluous `@pytest.mark.rsync` mark from the
`test_troubleshoot_transcript_for_errors` e2e tutorial test. That test creates a
local `command` agent with `--no-connect` and then runs `mngr transcript`, which
fails fast on a client-side agent-type check; no file sync (rsync) ever runs, so
the resource guard rightly flagged the mark as never invoked. The `@pytest.mark.tmux`
mark is retained because the local agent's tmux session is set up at create time.
No user-facing behavior change.
