`mngr kanpan --format json` now prints a single board snapshot instead of launching the TUI, for programmatic use. The JSON has the ordered columns, agents grouped into sections (with human labels), and any fetch errors; each agent carries both the pre-rendered cells (text/url/color) and the structured field values (PR number, CI status, commits-ahead count, etc.).

`--format jsonl` is also supported: it emits one agent record per line in board order, followed by any error lines.

Previously `--format json` was accepted but silently ignored.
