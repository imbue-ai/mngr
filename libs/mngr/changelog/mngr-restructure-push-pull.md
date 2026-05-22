### Restructure `mngr push` and `mngr pull` into `mngr rsync` and `mngr git push`/`mngr git pull`

The experimental `mngr push` and `mngr pull` commands combined three different
primitives (rsync, git push, git pull) behind `--sync-mode={files,git}` and
`--rsync-only` flags. They are replaced by three thin primitives that each wrap
a single operation:

- `mngr rsync SOURCE DESTINATION` — wraps rsync. Exactly one of `SOURCE` /
  `DESTINATION` must reference a remote agent or host; the other must be a
  local path. Local-to-local and remote-to-remote transfers are rejected.
- `mngr git push TARGET` — wraps `git push` from the current working
  directory's repo to a remote agent or host's repo. `TARGET` must include an
  agent or host (bare local paths are rejected; use plain `git push`).
- `mngr git pull SOURCE` — wraps `git pull` from a remote agent or host's repo
  into the current working directory's repo.

Each new subcommand only accepts the flags that apply to its underlying
operation (no more `--sync-mode`, `--rsync-only`, or unused git flags on the
rsync command). Endpoints are passed positionally as `HostLocationAddress`
values, matching the rest of the CLI.

`mngr push` and `mngr pull` are removed (no compatibility shim).

API-level changes in `imbue.mngr.api.sync`: `pull_files`/`push_files`/`pull_git`/`push_git`
are replaced by `rsync_from_remote`, `rsync_to_remote`, `git_pull`, and
`git_push`. There is also a top-level `rsync(source_host, source_path,
destination_host, destination_path, ...)` for the two-endpoint shape used by
the CLI. The `SyncMode` enum and the `mode` field on result classes are gone;
`SyncFilesResult` is renamed to `RsyncResult` and `SyncGitResult` to
`GitSyncResult`.
