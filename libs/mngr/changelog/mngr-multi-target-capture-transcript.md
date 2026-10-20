`mngr capture` and `mngr transcript` accept several targets in one call (`mngr capture A B C --full --no-start`, `mngr transcript A B C --format jsonl`). All targets resolve against a single discovery pass, so reading many agents costs one CLI start rather than one per agent (12 pane captures: 2.2s in one call against 20.7s as separate calls).

- A target that fails (an unknown agent, a stopped agent under `--no-start`, an agent type without transcripts) reports its own error and the remaining targets still produce output; the exit code is 1 if any target failed.

- With more than one target, human output puts a `==> NAME <==` header before each target's block, and `--format json`/`jsonl` emits one record per target: `{"target", "agent_id", "name", "error", "content"}` for capture and `{"target", "agent_id", "name", "error", "events"}` for transcript (the stream's records, unmodified). JSONL writes each record as soon as its target is done.

- `mngr transcript`'s `--format atif` and `--output` take a single target.

- A single target's output and behavior are unchanged.
