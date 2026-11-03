# Hook payload sequences

Hook payloads captured from the real `grok 1.0.62` binary (pinned build, stub model server), merged with the `turn_completed` records of the same sessions' `updates.jsonl`.
`grok_hook_test.py` replays them through `grok_hook.py`, feeding each `turn_completed` as the transcript streamer's `--turn-completed <sessionId> <promptId>` call.

## Format

One JSON object per line:

- line 1, `{"kind": "labels", ...}`: the binary version, the capture it came from, and short labels for the session ids (`R`, `R2` for roots, `C1` for subagents) and prompt ids (`p1`, `C1.p1`, `wake-T1` for a background-shell wake, `wake-C1` for a subagent wake) that the tests use in their step descriptions;
- `{"kind": "hook", "t": <seconds since the first step>, "payload": {...}}`: a hook payload exactly as grok sent it on stdin;
- `{"kind": "turn_completed", "t": ..., "sessionId": ..., "promptId": ..., "stopReason": ...}`: a `turn_completed` record, placed at the time grok wrote it.

Steps are in the order observed.
Normalised: absolute scratch paths are replaced by `/scratch` (also inside the URL-encoded session bucket of `transcriptPath`), and every payload `timestamp` by a fixed placeholder; the `t` field keeps the timing.
Session ids, prompt ids, prompts and all other fields are verbatim.

## Files

| File | Shows |
|---|---|
| `plain.jsonl` | One plain turn; `idle_prompt` 60 s after it; quit (`SessionEnd`, then `Stop{shutdown}`). |
| `tool.jsonl` | One turn with a foreground tool call (`PreToolUse`, `PostToolUse`). |
| `fg_subagent.jsonl` | Plain, two tool turns, a foreground subagent turn (the child's `SessionEnd` arrives while the parent's turn runs, before the spawn's `PostToolUse`), quit, `--resume` (`SessionStart{load}`) and one more turn. |
| `bgsubagent.jsonl` | A background subagent outlives its spawning turn (the `Stop` snapshot lists it); it ends while the parent is idle, and the `subagent-completed-<id>` wake turn follows 69 ms later. |
| `bgsubagent_busy_root.jsonl` | The same child ends while the parent runs a second user turn; the wake turn starts 61 ms after that turn's `turn_completed`. |
| `bgshell.jsonl` | A background shell outlives its turn; `Notification{task_complete}` and the `task-completed-<id>` wake turn. |
| `interrupt.jsonl` | Two turns cancelled with Ctrl+C (mid-stream, mid-tool): `turn_completed{cancelled}` precedes `StopCancelled` by 39-65 ms; then a normal turn. |
| `send_now.jsonl` | A queued message sent with "send now": the cut turn's `turn_completed{cancelled}` (no `Stop`, no `StopCancelled`) 62 ms before the queued message's `UserPromptSubmit`. |
| `stop_block.jsonl` | A `Stop` hook that blocks once per prompt: two `Stop{end_turn}` per prompt id (`stopHookActive` false, then true), one `turn_completed` after the second; also on a background-shell turn and its wake. |
| `idle_prompt.jsonl` | `GROK_IDLE_NOTIFICATION_DELAY_MS=3000`: `idle_prompt` after turns, suppressed by a turn sent 2 s after the previous one, and after a cancelled turn. |
| `silent_wake_exhaust.jsonl` | A background-shell wake whose model reply is empty: no `Stop` for 327 s of retries, then `Notification{agent_error}`, `StopFailure` 12 ms before `turn_completed{error}`. |
| `new_session.jsonl` | A turn, `/new` (`SessionStart{new}` for a second root) and a turn there, quit (both roots end), `--resume` of the first root and a turn. |
| `unknown_session.jsonl` | `SessionEnd` and `Stop{shutdown}` for a session that never had `SessionStart` (seen while a consent notice re-armed at launch). |
