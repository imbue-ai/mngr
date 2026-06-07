Minds now stores its state under platform-canonical directories instead of a
single `~/.minds/` (or `~/.minds-<tier>/`) dotfolder.

State is split across four roots, each with the tier (`production` / `staging` /
`dev-<name>`) as its first subdirectory:

- macOS: `~/Library/Application Support/Minds/<tier>/` (secrets, sessions, mngr,
  ssh, telegram, latchkey, backups), `~/Library/Caches/Minds/<tier>/`
  (regenerable caches), `~/Library/Logs/Minds/<tier>/` (logs), and
  `<app-support>/config/` (config).
- Linux: the XDG data / cache / state / config dirs
  (`~/.local/share/minds/<tier>/`, `~/.cache/minds/<tier>/`,
  `~/.local/state/minds/<tier>/logs/`, `~/.config/minds/<tier>/`), honoring
  `XDG_*_HOME`.
- `MINDS_DATA_HOME` overrides everything to
  `$MINDS_DATA_HOME/<tier>/{app_support,cache,logs,config}/`.

A legacy `~/.minds*` install is migrated onto the new roots automatically on the
first launch of a new build. The migration is idempotent, crash-safe, and
non-destructive: it moves known subdirs to their new roots, rewrites the
absolute paths embedded in `mngr/profiles/*/data.json` (e.g. docker SSH keys),
records a `migration.lock`, and leaves the old directory in place with a
`MIGRATED.txt` pointer plus backwards-compat symlinks at the old `mngr/` and
`ssh/` paths so already-running Lima VMs keep working.

New: `minds env teardown [<env>]` deletes an env's local data roots after
confirmation (a clean uninstall path; does not touch cloud resources). The
README gains "Where Minds stores its data" and "Uninstalling Minds" sections.
