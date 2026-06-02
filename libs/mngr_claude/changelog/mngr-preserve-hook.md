Claude session preservation on destroy was rewritten onto the new shared
`preserve_agent_data` machinery in core mngr. Behavior is unchanged in substance -- session
JSONLs, the raw and common transcripts, and the session-id history are still preserved before
the agent state directory is deleted, and `projects/` is still skipped in `use_env_config_dir`
mode -- but the implementation is now a single declarative list of files preserved through one
code path for both online and offline (volume-backed) hosts, replacing the previously
duplicated SSH and Volume implementations.

The on-disk layout of preserved sessions changed: files now live at
`<local_host_dir>/preserved/<agent-name>--<agent-id>/` and mirror the agent state directory
verbatim (e.g. `plugin/claude/anthropic/projects/...`, `logs/claude_transcript/...`,
`events/claude/common_transcript/...`, `claude_session_id_history`), instead of the old
`<local_host_dir>/plugin/mngr_claude/preserved_sessions/<agent-name>--<agent-id>/` location
with renamed subdirectories. This is a switch-forward change; previously preserved sessions in
the old location are left in place.
