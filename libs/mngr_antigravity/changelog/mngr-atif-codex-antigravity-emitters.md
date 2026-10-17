The antigravity common-transcript emitter now writes ATIF-shaped stream records
(`header` / `step` / `observation`) instead of the legacy
`user_message` / `assistant_message` / `tool_result` envelopes, per
`specs/atif-transcript-alignment/spec.md`. Every stream opens with a
`header` line pinning `ATIF-v1.7`, and the envelope's `source` field is
renamed `emitter` (ATIF claims `source` for the step originator).

Full fidelity: a tool call's `arguments` is the complete decoded object (agy
serializes them as a JSON string; anything that does not parse to an object
rides whole under `_raw`) and `CODE_ACTION` output is recorded untruncated.
The 200-character input previews and 2000-character output cap are gone;
truncation is now purely a display-time concern in `mngr transcript`.

Newly captured: the planner's thinking, which the SQLite decoder already reads
off `CortexStepPlannerResponse` and hangs on the `PLANNER_RESPONSE` record,
now becomes that agent step's `reasoning_content` instead of being discarded.

agy's own per-conversation annotations (`conversation_id`, `step_index`) moved
under the ATIF `extra` objects -- on the step for user and agent steps, and on
the result for observations -- since every other field on a record must be an
ATIF field for the doc-builder to assemble documents mechanically.

Records are now appended in the order agy wrote them rather than sorted by
timestamp. A `CODE_ACTION` could previously be emitted *before* the
`PLANNER_RESPONSE` that called the tool: when the decoder degraded a step's
`created_at`, the converter substituted conversion time, which sorts after every
recorded event. The reader treats append order as authoritative, so that tool
output was dropped from the assembled transcript.

A step whose `created_at` the decoder could not read (it degrades those to an
empty string) now carries conversion time as its timestamp instead of failing
the ATIF requirement that every step and observation record have one.
