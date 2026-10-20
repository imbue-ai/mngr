The shared agent-plugin release harness (`agent_release_testing.py`) now asserts
against the ATIF-shaped common-transcript records that all five emitters write:
the envelope check reads `emitter` rather than `source`, seed-turn and delivery
helpers key on `step` records with `source: "user"`, recall keys on agent steps,
usage is asserted on at least one agent step carrying `metrics.prompt_tokens` /
`completion_tokens` / `cached_tokens` (claude omits `metrics` entirely on lines
that report no usage), forced tool calls are found under an agent step's
`tool_calls[].function_name`, and tool results are expected as `observation`
records.
