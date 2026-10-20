The claude common transcript now emits the ATIF-shaped stream records described in `specs/atif-transcript-alignment/spec.md`, replacing the bespoke `user_message` / `assistant_message` / `tool_result` records. Every stream starts with a `header` line pinning the ATIF revision, and each following line is a `step` or an `observation`.

The stream is now full fidelity: tool `arguments` are the complete input objects (no more 200-char previews), tool outputs and stop-hook text are untruncated (no more 2000-char cap), and the model's thinking is captured as `reasoning_content`. Display truncation lives in `mngr transcript` instead, with `--full` to disable it.

Claude fans one API response out over several transcript lines; those lines are now grouped by their shared `message.id` into a single agent step, so one step is one LLM inference and its token usage is counted once instead of once per line. Because those lines arrive over several seconds, the converter holds back the last inference until a later line proves it finished; the turn-end flush (`--single-pass`) knows the turn is over and emits it.

Framework-injected `isMeta` records (stop-hook output, local-command caveats) are now real system steps rather than fake `meta` tool results, and a context compaction is recorded as a system step carrying the ATIF v1.7 `context_management` convention with the summary on its inline observation.

Each agent step also records `llm_call_count: 1` (one step is one inference by construction), and a native Task subagent's records -- which claude interleaves into the same session file with `isSidechain: true` -- are grouped in their own lane and marked `is_sidechain` in the step's (or observation result's) `extra`, so a subagent's turn can no longer split the main thread's inference in two.

The trade-off of holding the last inference back: mid-turn, the newest in-flight inference appears in the common transcript only once it is provably complete (the next record that closes it, or the turn-end flush), so a live `mngr transcript --follow` tail lags the agent by up to one inference.
