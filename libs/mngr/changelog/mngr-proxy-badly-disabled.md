Added reusable gitignore-status helpers (with a `GitignoreStatus` result) to `mngr.api.git`. Given a host, a repo path, and any repo-relative path (which need not exist yet), they report whether that path is gitignored -- resolving symlinks anywhere along the path (e.g. `.claude -> .agents`) first so `git check-ignore` doesn't choke with "beyond a symbolic link":

- `check_path_gitignore_status` -- ignored by any rule (returns `SKIP` / `IGNORED` / `NOT_IGNORED`).

- `check_path_repo_gitignore_status` -- same, but a path ignored only by the user's global excludes returns `ONLY_GLOBAL` rather than `IGNORED` (for preflight checks whose result must also hold on a remote host / fresh clone, which has no global excludes).

Plugins use these to guard files they write into an agent worktree against showing up as untracked changes.
