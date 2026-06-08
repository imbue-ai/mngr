Fixed the `test_recipe_multi_agent_parallel_workflow` e2e release test for the
MULTI-AGENT WORKFLOWS tutorial recipe:

- Added a `@pytest.mark.timeout(120)` override so the multi-step recipe (which
  creates three command agents plus list/wait/exec/msg/merge/destroy) is not
  killed by the default 10s per-test timeout, and removed the superfluous
  `@pytest.mark.modal` mark since the test exercises local command agents only.
- Corrected the tutorial's "message all agents" step: `mngr msg -a` is not a
  valid option. It now uses the documented idiom
  `mngr list --ids | mngr msg - -m "..."` (updated in `mega_tutorial.sh` too).
- Strengthened the test to verify actual behavior rather than just exit codes:
  all three agents are created and isolated in distinct worktrees, the
  broadcast message reaches all three, and cleanup removes them.
