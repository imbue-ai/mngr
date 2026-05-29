`mngr snapshot destroy` now supports a `--dry-run` flag that reports which
snapshots would be destroyed without actually deleting anything. This matches
the behavior already documented in the tutorial and offered by other destroy
commands (`mngr destroy --dry-run`, `mngr gc --dry-run`).
