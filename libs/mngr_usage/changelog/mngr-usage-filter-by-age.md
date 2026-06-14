README: add a "Filtering by event age" section documenting `--since` as the way to bound the per-session cost aggregation by event age, and clarifying that `--max-age` is a stale-warning threshold, not a filter.

`mngr usage --help`: move `--since`, `--max-age`, `--detail`, and `--preserved/--no-preserved` out of "Ungrouped". `--since` and `--preserved/--no-preserved` now render under the existing "Filtering" group (matching `mngr usage wait`, where `--preserved/--no-preserved` already lives); `--max-age` and `--detail` render under a new "Display" group (matching the convention in `mngr transcript` / `mngr events`).

`mngr usage --help` synopsis: enumerate the options unique to `mngr usage` (`--max-age`, `--detail`, `--since`, `--no-preserved`) instead of the placeholder `[OPTIONS] [COMMAND]`, matching the style used by `mngr usage wait` and other `mngr` commands (which omit shared filter options like `--include` / `--provider`).
