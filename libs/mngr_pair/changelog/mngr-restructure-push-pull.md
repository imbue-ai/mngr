### Migrate to the new thin `git_pull`/`git_push` wrappers

`mngr_pair` now composes the thin `git_pull`/`git_push` wrappers (from
`imbue.mngr.api.sync`) with its own stash guard and target-branch checkout
dance. Externally observable behavior of `sync_git_state` (stash mode handling,
target-branch handling) is unchanged.
