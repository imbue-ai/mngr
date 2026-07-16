Fixed minor minds dev-setup papercuts:

`just minds-start`'s "no minds env activated" error now suggests the correct env name form `dev-<your-user>` (was `<your-user>-dev`, which the env-name regex rejects), and points out that `--create` is required on the first activation of a fresh dev env. The `minds-start` recipe's doc comment got the same correction.

`just default-workspace-template-worktree` no longer fails with `fatal: 'HEAD' is not a valid branch name` when run from a jj (jujutsu) colocated checkout. It defaulted the new checkout's branch to `git rev-parse --abbrev-ref HEAD`, which returns the literal `HEAD` in the detached-HEAD state jj normally leaves git in. It now falls back to jj's nearest bookmark to the working copy (`@`) when git HEAD is detached, and otherwise errors with a clear hint to pass the branch explicitly.
