Consolidated the docs describing how FCT's `vendor/mngr` is kept in sync, so the `git archive` (release) vs `rsync` (dev/bake) mechanisms are explained in one place instead of being re-described in each consumer.

Fixed a stale cross-reference in the `justfile` `minds-start` recipe: the rsync-exclusions comment pointed at `_RSYNC_MANUAL_EXCLUDES` in `cli/admin.py` / `cli/pool.py`, but the constants are actually `_VENDOR_RSYNC_MANUAL_EXCLUDES` / `_GITIGNORE_RSYNC_FILTER` defined in `bake/pool_bake.py` (which admin/pool call). The comment now names the correct constants and file and points at the new canonical doc.

Trimmed the duplicated rsync-form explanation out of the `minds-dev-workflow` skill and pointed the `minds-justfile` skill's `sync-vendor-mngr` entry at `apps/minds/docs/vendor-mngr-sync.md`.
