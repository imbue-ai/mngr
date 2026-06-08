Fixed the `test_recipe_launch_check_cleanup` release e2e test (COMMON TASKS recipe block) so it passes against the current CLI. The test substitutes a local `command` (sleep) agent for the recipe's modal claude agent, and several recipe steps behave differently for that stand-in:

- Added the missing `@pytest.mark.timeout(300)` marker (all sibling tutorial e2e tests carry one); without it the test fell back to the global 10s default and timed out partway through.
- The `mngr transcript` step now asserts the real behavior for a `command` agent: command agents do not produce a common transcript, so the exact recipe command (`mngr transcript fix-bug --tail 3`) exits non-zero with a clear "does not produce a common transcript" message.
- The `mngr conn` step runs in the e2e harness without a TTY, so the interactive `tmux attach` cannot complete; the test now verifies the command resolves the named agent and reaches the connect step rather than asserting a clean exit.
- Removed the superfluous `@pytest.mark.modal` marker: the recipe substitutes a local command agent and never invokes Modal, which tripped the resource-guard "marked modal but never invoked modal" check.

Also strengthened the test's verifications: it now confirms the created agent is genuinely alive in its own worktree (`mngr exec fix-bug pwd`) -- the concrete intent of the "check what agents are running" step, which `mngr list --running` alone could not show for the idle `sleep` stand-in (it reports WAITING, not RUNNING) -- and confirms `mngr destroy --remove-created-branch` actually removed the agent's branch.
</content>
