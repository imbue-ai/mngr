Fix and extend the e2e tutorial test for `mngr exec my-task "git branch --show-current"`.

The `test_exec_branch_show_current` release test was marked `@pytest.mark.rsync`, but `git branch --show-current` is a read-only command that never invokes rsync. The resource guard correctly failed the otherwise-passing test for carrying a superfluous mark; the mark has been removed.

Added a companion test, `test_exec_branch_show_current_after_checkout`, that covers the scenario the tutorial comment calls out ("it may have shifted if the agent checked out a new branch"): after the agent checks out a brand-new branch, `git branch --show-current` reports the shifted branch rather than the original `mngr/{agent_name}` branch.
