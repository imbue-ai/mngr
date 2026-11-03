# grok session fixtures

Real `updates.jsonl` session logs written by the pinned grok **1.0.62** binary, driven by a
stub model server (replies start with `STUB_REPLY`, `STUB_CHILD` or `STUB_WAKE_REPLY`), with
logging hooks installed as `global/log` (and `global/stopblock`, `global/mngr` where noted).

Each scenario directory holds one `<session id>/updates.jsonl` per session and a `roots` file
listing the top-level session ids in the order `SessionStart` would append them to
`plugin/grok/roots`. Any other session directory is a child (subagent) session, named by a
`subagent_spawned` record in its parent.

Normalisation: absolute capture paths were rewritten to `/tmp/grok-fixtures/...` (also in their
percent-encoded bucket form `%2Ftmp%2Fgrok-fixtures...`). Nothing else was changed, so byte
offsets are those of these files, not of the original captures.

| Scenario | What it shows |
|---|---|
| `plain` | One prompt, one reply, `turn_completed{end_turn}` with usage. |
| `tool` | A `run_terminal_command` call: `tool_call`, a status-less `tool_call_update`, the completed update, then the follow-up reply. |
| `interrupt` | Ctrl+C mid-stream (`turn_completed{cancelled}` without usage), Ctrl+C during a foreground tool (the killed tool never gets a terminal update), then a normal turn. |
| `send_now` | A turn cut by send-now (`cancelled`, `cancelTrigger: send_now`) and the queued follow-up as the next turn. |
| `stop_block` | A Stop hook (`global/stopblock`) that blocks once per prompt: a failed, `blocked: true` run, a `hook_annotation`, the continuation reply, and one `turn_completed`; also a background shell and its blocked auto-wake turn. |
| `background_shell` | A background shell (`task_backgrounded`, `background_tasks`, `task_completed`) and the hidden `task-completed-<id>` wake prompt with its reply. |
| `background_subagent` | A background subagent: `subagent_spawned`, the child session (its own `turn_completed`), `subagent_finished`, and the hidden `subagent-completed-<id>` wake turn. |
| `failed_wake` | An auto-wake turn whose model replies stay empty: 15 `retry_state` records, then `turn_completed{error}` with `agent_result` and no reply. |
| `foreground_subagent_failing_hooks` | Hooks registered as `global/mngr` that exit 1 (`hook_execution` runs with `status: failed`) in the root and in a foreground subagent's child session, then the same turns with the hooks succeeding. |
| `new_session_and_resume` | A prompt, `/new` (a second root with its own prompt), quit, then `--resume` of the first root and another prompt in it. |
