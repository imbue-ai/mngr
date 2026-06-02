Updated the destroyed-agent fallback to read the preserved common transcript from its new
location. Preserved Claude sessions now mirror the agent state directory under
`<local_host_dir>/preserved/<agent-name>--<agent-id>/`, so the common transcript is read from
`preserved/<name>--<id>/events/claude/common_transcript/events.jsonl` (via the shared
`get_preserved_agent_dir` helper) instead of the former
`plugin/mngr_claude/preserved_sessions/<name>--<id>/common_transcript/events.jsonl`.
