Fixed workspace creation failing when the source repository's requested branch is not the default branch.

- Cloning a remote repo for a non-default branch previously failed with `pathspec '<branch>' did not match any file(s) known to git`. The remote clone used `git clone --depth 1`, which (implying `--single-branch`) fetches only the default branch, so the requested branch's ref was never downloaded and the subsequent checkout could not find it.
- `clone_git_repo` now takes an optional `branch` and, when given, clones with `--single-branch --branch <branch>`: only that branch is fetched (still cheaper than a full clone) but its complete, non-shallow history is present. The remote create path passes the requested branch through.
- The shallow (`--depth 1`) clone is gone entirely. Besides the checkout failure, a shallow clone could not be mirror-pushed into the agent container's bare repo (`mngr create` rejects it with "shallow update not allowed"); a single-branch clone keeps the full ancestry that push requires.
- Requesting a branch that does not exist on the remote now fails cleanly at clone time rather than later at checkout.
