Added `mngr state <target>`: a cheap, single-target status command. Unlike `mngr list` (which enumerates every provider and then filters), `state` resolves just the one agent or host -- querying only its provider -- so it stays fast even with many agents, as long as you know which one you want.

- For an agent target it shows the full agent details (the same fields as `mngr list`, including host info). For a host target it shows the host details plus the agents running on it (by name, without per-agent detail collection).

- `--quick` reports only the lifecycle state (agent + host) without the full detail fetch, skipping plugin field generators -- cheaper, handy for scripting.

- Output honors `--format` (human, json, jsonl, or a template like `'{name} {state}'`) and, in human mode, `--fields` to choose which fields to display -- mirroring `mngr list`.

New core API in `imbue.mngr.api.agent_state` backs the command: `get_agent_details` / `get_host_details` (rich, addressed lookups) and `resolve_target` / `poll_combined_state` (the cheap lifecycle poll, shared with `mngr wait`).
