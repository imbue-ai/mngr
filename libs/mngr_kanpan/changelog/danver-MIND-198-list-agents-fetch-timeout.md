Fixed a CI flake (MIND-198) in the mute write-semantics acceptance test
(`test_set_agent_mute_writes_the_state_it_is_given`). It verified persistence by
running a full `fetch_board_snapshot` after each write, and a board fetch shells
out to probe every agent's tmux lifecycle; doing that three times could exceed the
test's 10s timeout on a loaded CI sandbox. The test now reads the flag straight
back from the agent's persisted plugin data -- exactly what `set_agent_mute`
writes -- so it no longer depends on the board-read pipeline, and a stand-in
`tmux` on PATH pins the read-back off that path. No product behavior changed; the
board's own surfacing of the muted flag stays covered by the sibling
`fetch_board_snapshot` tests.
