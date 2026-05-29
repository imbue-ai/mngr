Fixed the tutorial example for using JSON/JSONL output across commands. The
previous example (`mngr snapshot list --format json`) always failed because
`snapshot list` requires an agent or host target; it now uses
`mngr config list --format json` instead, which runs with no setup. Strengthened
the corresponding e2e test to verify the JSON and JSONL output is actually
parseable, and added a companion test asserting that `--format json` and
`--format jsonl` produce the documented structurally-different output (a single
JSON document vs. one object per line) for the same command.
