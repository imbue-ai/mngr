Fix creating a mind from a remote git URL (e.g. a GitHub HTTPS URL) when no branch is specified.

Cloning a remote repo without an explicit branch left the local clone on a detached HEAD (the no-branch path checked out `FETCH_HEAD` detached, and -- unlike the branch-given path -- nothing renamed it to a real local branch afterward). That left `refs/heads/*` empty, so the downstream `mngr create` mirror push, which only pushes `refs/heads/*` + `refs/tags/*`, failed with `No refs in common and none specified; doing nothing` / `the remote end hung up unexpectedly`.

The no-branch clone now uses a plain `git clone`, which resolves the remote's default branch natively and leaves a real named local branch checked out (whatever the remote's default is -- `main`, `master`, etc.). The explicit-branch path is unchanged (it still uses `git fetch` so that a branch, tag, or commit SHA all work).
