Claude agents now report when they are compacting their context, so consumers such as the workspace chat app can show a status while a compaction runs.

- A new `PreCompact` hook writes a `compacting` marker to the agent state dir, holding `{"trigger": "manual"|"auto"|"unknown", "started_at": "<utc iso>"}`, and emits an activity event. It fires for a `/compact` and for Claude Code's own auto-compaction.

- A new `PostCompact` hook writes `last_compaction.json` (`{"trigger": ..., "ended_at": ...}`) to the agent state dir, removes the `compacting` marker, and emits an activity event. The summary is not copied; it stays in the transcript.

- Both hooks always exit 0, so a missing `jq`, an unwritable state dir or a malformed payload can never block or interrupt a compaction. A payload without a recognised `trigger` records `"unknown"`.

- The startup/resume `SessionStart` hook also removes a stranded `compacting` marker. A compaction cancelled with Escape, or one whose summarization request fails, fires no `PostCompact`, so the marker can be left behind; consumers should also treat an old marker as stale.

- Hooks are written only when an agent is created, so agents created before this change never write these files.

- Autocompact no longer compacts a Claude agent a second time right after a compaction. A compaction writes no usage record, so the context size used to stay at the pre-compaction total and the agent still looked over `min_context_tokens`. Now a `compact_boundary` record that is newer, in file order, than every assistant usage makes `get_context_tokens()` return `None`, and the agent is left alone until its next reply reports a real size. The boundary's own `postTokens` estimate is not used as the size: on 42 real compactions the next turn's usage was 2.0 to 8.4 times `postTokens` (42k to 101k tokens more), because the estimate leaves out the session's fixed overhead. The idle timestamp is unaffected, and a suppressed size is logged at debug level with the agent and transcript path. The new `is_compact_boundary_record` identifies the boundary record.
