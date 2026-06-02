Stopped `antigravity` agents now resume their prior agy conversation on restart, instead of starting a fresh one.

- A `PreInvocation` capture hook records the agent's active agy conversation ID (read from agy's hook payload, which carries `conversationId`) to a per-agent file. On `mngr start`, the launch command resumes the most-recently-active conversation via `agy --conversation <id>`, so the agent keeps its full context across a stop/start. The resume is shell-evaluated at launch (the stored command is replayed on each start) and works under both bash and zsh.
- Resume is guarded on agy's incremental `conversations/<id>.db` store still existing -- that file survives the hard process kill `mngr stop` performs and is what agy resumes from (the `.pb` snapshot is only written on a clean in-TUI exit). If the conversation is gone, the agent launches fresh rather than erroring.
- Note: agy's `--conversation` only resumes an existing conversation; it cannot mint a caller-supplied ID. mngr therefore lets agy assign the ID and captures it via the hook.

The transcript streamer now discovers this agent's conversation IDs from the same capture-hook file rather than grepping agy's `--log-file`. This is the single source of truth for conversation IDs (shared with resume), and it removes a latent bug where resumed conversations were missed because their log line reads `Resuming conversation` (not the `Resumed conversation` the streamer matched).

Clone-resume (making a cloned antigravity agent continue the source's conversation) is not included here -- agy's conversation store is global rather than per-agent, so it needs separate handling and is left for a follow-up.
