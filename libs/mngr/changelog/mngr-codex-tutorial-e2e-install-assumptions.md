Three codex tutorial e2e release tests no longer assume `mngr create --no-auto-start` leaves the agent unstarted. That flag governs starting offline *hosts*; the create always launches the agent, so a codex agent on a host with no codex binary cannot come up.

`test_create_codex_explicit_type` no longer turns off `agent_types.codex.check_installation`. With the check off, provisioning skipped the codex install and the launch then failed with `codex app-server did not become ready within 10s`. Letting the install run is what the two sibling codex tutorial tests already do. `codex app-server` completes mngr's readiness handshake without being logged in, so the test still needs no codex credentials.

All three codex tutorial creates now get the same widened per-command budget, and so does each test's `mngr list` read-back of the created type. Every create is its test's first `mngr` invocation and installs the codex CLI when the host has none, and computing a just-launched codex agent's live state is occasionally slow, so the 30s default left both commands near the ceiling.

`test_create_codex_agent` is no longer marked `@pytest.mark.rsync`. The source repo is clean, so the worktree transfer finds no untracked or modified files to copy and never invokes rsync, and the mark's resource guard failed the test for never invoking rsync even though the test itself passed.
