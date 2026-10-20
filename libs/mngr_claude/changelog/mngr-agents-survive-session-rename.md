A claude agent's background tasks (activity tracking, transcript streaming, the common transcript converter and the response-streaming watcher) keep running after the agent is renamed, including after it restarts. The supervisor tracks the agent's tmux session by its session id, which a rename does not change, and new launch commands no longer carry the session name.

- An agent created by an older mngr keeps its supervisor script, which follows the session name it is given. `mngr start` rewrites that name out of its stored launch command, so after its next restart its background tasks run for its current session (and survive restarts); a live rename still ends them until the next restart.

- The background-tasks and response-streaming pidfiles are keyed on the agent id, so a new agent that takes a renamed agent's old name gets its own watchers.
