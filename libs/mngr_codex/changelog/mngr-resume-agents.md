Codex agents can now adopt an existing codex session at create time, so a fresh agent resumes that conversation instead of starting empty.

The session to adopt is resolved from a session id (or an absolute rollout `.jsonl` path) across three stores: the user's native `~/.codex/sessions`, every live local mngr codex agent, and every preserved (destroyed) codex agent. An id matching in more than one store is rejected as ambiguous, with a clear message telling you to pass the full rollout path.

On adoption, the resolved rollout store is copied into the new agent's `CODEX_HOME/sessions`, the recorded working directory inside the rollout (the `session_meta` and `turn_context` records) is rewritten to the new agent's work dir -- so `codex resume` does not pop the "Choose working directory to resume this session" modal -- and the adopted session id is written as the agent's resume pointer.

This is driven by the central `mngr create --adopt-session <id>` flag (repeatable; the last value wins for codex), which reaches the codex agent via `plugin_data["adopt_session"]`.
