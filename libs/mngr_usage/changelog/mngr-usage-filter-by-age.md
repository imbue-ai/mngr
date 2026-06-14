README: add a "Filtering by event age" section documenting `--since` as the way to bound the per-session cost aggregation by event age, and clarifying that `--max-age` is a stale-warning threshold, not a filter.

`mngr usage --help`: move `--since`, `--max-age`, `--detail`, and `--preserved/--no-preserved` out of "Ungrouped" into "Aggregation" (`--since`, `--preserved/--no-preserved`) and "Display" (`--max-age`, `--detail`), matching the option-group convention used by `mngr usage wait` and other mngr commands.
