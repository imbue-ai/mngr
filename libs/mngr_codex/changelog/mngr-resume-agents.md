Codex agents can now adopt an existing codex session at create time, so a fresh agent resumes that conversation instead of starting empty.

The session to adopt is resolved from a session id (or an absolute rollout `.jsonl` path) across three stores: the user's native `~/.codex/sessions`, every live local mngr codex agent, and every preserved (destroyed) codex agent. An id matching in more than one store is rejected as ambiguous, with a clear message telling you to pass the full rollout path.

On adoption, the resolved rollout store is copied into the new agent's `CODEX_HOME/sessions`, the recorded working directory inside the rollout (the `session_meta` and `turn_context` records) is rewritten to the new agent's work dir -- so `codex resume` does not pop the "Choose working directory to resume this session" modal -- and the adopted session id is written as the agent's resume pointer.

This is driven by the central `mngr create --adopt <id>` flag (repeatable). `--adopt-session` is still accepted as an alias. The codex plugin now reads the values from the first-class `CreateAgentOptions.adopt_session` field rather than from `plugin_data["adopt_session"]`. A bad or ambiguous id is still caught up front (before any host or worktree is created) as a clean error rather than a traceback.

Multiple `--adopt` values are now each copied into the new agent (their date-nested rollouts coexist, so all are available in codex's session switcher), and the last one named is the conversation actually resumed.

Cloning a codex agent with `mngr create <new> codex --from <agent>` now resumes the source agent's conversation too: the clone transfers the source's native session store, resumes its most-recent rollout, and rebinds the recorded working directory to the clone's work dir (so no resume modal appears). `--adopt` and `--from` may now be combined -- every named session plus the clone is made available, and the clone's conversation is the one resumed. When a `--from` clone has no resumable codex session (no store, or a store with no rollout), agent creation now fails with a clear error instead of silently starting a fresh session, since `--from` is an explicit request to resume that agent.

A failure to resolve the user's `CODEX_HOME` during provisioning now surfaces as a clean, user-facing error instead of an abrupt process exit.
