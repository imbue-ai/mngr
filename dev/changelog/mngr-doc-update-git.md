Consolidated the docs describing how FCT's `vendor/mngr` is kept in sync, so the `git archive` (release) vs `rsync` (dev/bake) mechanisms are explained in one canonical place (`apps/minds/docs/vendor-mngr-sync.md`) instead of being re-described in each skill.

Trimmed the duplicated rsync-form explanation out of the `minds-dev-workflow` skill and pointed the `minds-justfile` skill's `sync-vendor-mngr` entry at the canonical doc.
