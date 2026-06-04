Ignore local scratch shell scripts: added a general `**/*.local.sh` rule to `.gitignore` (mirroring the existing `**/*.local.md`), so any `*.local.sh` helper script stays untracked. This subsumes the previous single-file `**/scripts/notify_user.local.sh` entry, which was removed.

Also broadened the identify-* `_tasks/` ignore rule from `*/*/_tasks/` to `**/_tasks/`, so the `dev` project's root-level `dev/_tasks/` output folder is ignored consistently with the `libs/<name>/_tasks/` and `apps/<name>/_tasks/` ones (the old two-level glob missed it).
