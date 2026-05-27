### Restructure `mngr push` and `mngr pull` into `mngr rsync` and `mngr git push`/`mngr git pull`

The experimental `mngr push` and `mngr pull` commands combined three different
primitives (rsync, git push, git pull) behind `--sync-mode={files,git}` and
`--rsync-only` flags. They are replaced by three thin primitives that each wrap
a single operation:

- `mngr rsync SOURCE DESTINATION` — wraps rsync. Exactly one of `SOURCE` /
  `DESTINATION` must reference a remote agent or host; the other must be a
  local path. Local-to-local and remote-to-remote transfers are rejected.
- `mngr git push TARGET [-- GIT_ARGS...]` — thin wrapper around `git push`
  from the current working directory's repo to a remote agent or host's repo.
  Anything after `--` is passed verbatim to the underlying `git push`.
- `mngr git pull SOURCE [-- GIT_ARGS...]` — thin wrapper around `git pull`
  from a remote agent or host's repo into the current working directory's
  repo. Anything after `--` is passed verbatim to the underlying `git pull`.

The git push/pull commands are thin pass-through wrappers: mngr resolves the
endpoint, builds the SSH URL with mngr's managed credentials, sets
`receive.denyCurrentBranch=updateInstead` on push targets, and adds a
`safe.directory` entry — then runs vanilla `git push` / `git pull` with any
flags the user supplies after `--`. The mngr-side flags
`--source-branch`/`--target-branch`/`--mirror`/`--uncommitted-changes`/`--dry-run`
are gone; use the corresponding git flags directly (`feature:main` refspec
syntax, `--force --tags refs/heads/*:refs/heads/*` for a mirror push,
`--dry-run`, `--rebase`, etc.).

`mngr push` and `mngr pull` are removed (no compatibility shim).

API-level changes in `imbue.mngr.api.sync`: `pull_files`/`push_files`/`pull_git`/`push_git`
are replaced by `rsync_from_remote`, `rsync_to_remote`, `git_pull`, and
`git_push`. There is also a top-level `rsync(source_host, source_path,
destination_host, destination_path, ...)` for the two-endpoint shape used by
the CLI. `git_push`/`git_pull` now take an `extra_args: Sequence[str]`
parameter and have no structured return value (raise `GitSyncError` on
failure). The `SyncMode` enum, `GitSyncResult`, and `NotAGitRepositoryError`
are gone; `SyncFilesResult` is renamed to `RsyncResult`.
