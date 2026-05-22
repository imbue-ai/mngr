### Migrate to the new `imbue.mngr.api.sync` interface

`mngr_tmr` now calls `rsync_from_remote` and `git_pull` (from
`imbue.mngr.api.sync`) instead of the removed `pull_files`/`pull_git` wrappers
from `imbue.mngr.api.pull`. Argument shape changes from `(agent, host,
destination, ...)` to `(remote_host, remote_path, local_path, ...)` for rsync
and `(local_path, remote_host, remote_path, ...)` for git pull; the agent's
`work_dir` is passed explicitly. Behavior is unchanged.
