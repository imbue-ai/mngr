Added the ATIF doc-builder: `mngr transcript <agent> --format atif` assembles an agent's ATIF-shaped common-transcript stream into a single validated ATIF-v1.7 trajectory document (optionally written to a file with `--output`). The merge rules follow `specs/atif-transcript-alignment/spec.md`: file order is authoritative, step ids are assigned sequentially, streamed observation results attach to the step that declared their tool call (unmatched results are preserved with a warning, never silently dropped), and the root is enriched with the agent type, id, and summed per-step metrics.

Claude subagents that mngr ran as sibling proxy agents are embedded recursively as ATIF v1.7 `subagent_trajectories` (marked `extra.subagent_kind: "mngr"`), with a `subagent_trajectory_ref` attached to the delegating tool call's result; a subagent whose delegating call has not produced a result yet is still embedded, under a result marked `extra.subagent_result_pending`, and a subagent that cannot be resolved leaves the plain textual result untouched.

`--format atif` goes through the same option machinery as every other format, so it can also be set as a config default (`[commands.transcript] output_format = "atif"`) or via the matching environment variable.

Old-format (pre-ATIF) streams are reported with a clear unsupported error.
