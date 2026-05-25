### permission-requests extension: preserve symlinks at the approval target

`POST /permission-requests/approve/<id>` previously replaced the
target `permissions.json` with a regular file when the target path
was a symlink. This broke the per-agent opaque symlinks that
`mngr latchkey link-permissions` swings into the canonical host
permissions file: subsequent agents sharing the canonical file
silently desynced from the granted permissions.

The atomic-write helper now `lstat`s the target and, if it is a
symlink, resolves it via `realpath` before computing the temp path
and renaming. The atomic swap lands on the underlying file and the
symlink stays in place. Approving against a non-symlink target,
or a target that does not yet exist, is unchanged.

`permissions.mjs` was already safe: its write paths resolve symlinks
up front via `resolvePathParamUnderRoot`.
