Fixed and strengthened the `test_start_multiple_agents` release test for the "start multiple agents at once" tutorial block.

Fixes: the test was hitting the default 10s pytest timeout while creating three agents, and it carried a superfluous `@pytest.mark.rsync` (creating/starting local command agents never invokes rsync). Added a generous `@pytest.mark.timeout(240)` and removed the `rsync` mark.

Improvements: the test now asserts the "Successfully started 3 agent(s)" summary line and execs into each of the three agents to confirm they are actually started and reachable in their own worktrees, rather than only checking that their names appear in the start command's output.
