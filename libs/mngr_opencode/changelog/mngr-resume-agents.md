Added session adoption for the opencode agent type: a newly created opencode agent can now resume an existing OpenCode conversation instead of starting fresh.

At create time the plugin resolves an adopt argument -- a `ses_...` session id (searched across the user-native opencode db and every live/preserved mngr agent's db) or an absolute path to a source `opencode.db` -- then copies the resolved SQLite db (and its `-wal`/`-shm` sidecars) into the new agent's data dir, checkpoints the WAL, rebinds the session's stored source-worktree path to the new agent's work dir (`session.directory`, `project.worktree`, and the `project_directory` upsert), and writes the adopted session id into the resume pointer so the agent's first launch attaches to it.

Triggered by the central `--adopt-session` flag (`mngr create opencode --adopt-session <id-or-db-path>`); OpenCode resumes a single root conversation, so when the flag is passed more than once the last entry is adopted. Parity with the claude adopt resolver: adoption works from a preserved (destroyed) agent, a live mngr agent, and a plain-CLI run.

A `--from <agent>` clone of an opencode agent now also resumes the source agent's conversation: a generic clone copies the source workspace but not its state dir, so the plugin transfers just the source's native opencode store (`opencode.db` + its `-wal`/`-shm` sidecars), reads the source's root session id from that store, rebinds it to the clone's work dir, and writes the resume pointer. If the source has no store, the clone starts a fresh session.

A bad or ambiguous `--adopt-session` id is now reported as a clean error before any host or worktree is created, rather than surfacing as a traceback during provisioning.
