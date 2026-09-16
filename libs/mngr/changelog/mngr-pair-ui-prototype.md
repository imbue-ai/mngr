`mngr pair` gained three flags, so its reference page and `--help` now list them:

- `--start` / `--no-start`, the standard flag other subcommands carry. It still defaults to starting an offline host; a caller that must never be what turns a stopped machine on (and bills for it) passes `--no-start`.

- `--ignore-archives`, for pairing two paths as though they had never been paired before. For a caller that just created one of the directories, where an archive from a previous pairing describes a directory that no longer exists — unison would otherwise read the fresh directory as a mass deletion and stop.

- `--source-path` now accepts an absolute path when pairing with a host rather than an agent, which is what `--source-host` makes possible. Its description says so.

- `--links` / `--no-links`, which decides whether symbolic links inside the two directories are synced. Defaults to `--links`, which is how pairing already behaved.
