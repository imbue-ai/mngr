Added an unhappy-path e2e test for the "collect results from all agents" tutorial block.

`test_advanced_collect_results_loop_missing_agent` runs the same `for agent in ...; do mngr exec "$agent" ...; done` loop but with one real agent and one name that was never created. It verifies the loop keeps iterating (both section headers print), the real agent still reports its git history, and the missing agent produces a clear "agent not found" error rather than silently succeeding, mis-parsing its command, or crashing the loop.
