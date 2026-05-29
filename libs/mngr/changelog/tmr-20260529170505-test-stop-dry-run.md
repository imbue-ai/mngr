`mngr stop` now supports `--dry-run`, which reports the agents that would be
stopped without actually stopping them (consistent with `mngr archive`,
`mngr cleanup`, and `mngr gc`). This matches the behavior documented in the
tutorial (`mngr list --ids | mngr stop - --dry-run`). The dry-run output
respects the `--format` option (human, json, jsonl, and format templates).
