### Migrate to the new `imbue.mngr.api.sync` interface

`mngr_pair` now calls `git_pull`/`git_push` (from `imbue.mngr.api.sync`) instead
of the removed `pull_git`/`push_git` wrappers from `imbue.mngr.api.pull` /
`imbue.mngr.api.push`. Argument shape changes from `(agent, host, source, ...)`
to `(local_path, remote_host, remote_path, ...)`; the agent's `work_dir` is
passed explicitly as `remote_path`. Behavior is unchanged.
