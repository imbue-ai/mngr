Add `--restart` flag to `mngr start` for cleanly restarting agents.

- `mngr start my-agent --restart` stops a running agent and starts it fresh without sending a resume message. If the agent is already stopped, it is simply started.
- Concurrent `--restart` calls for the same agent are deduplicated: the second call is a no-op while the first is in progress, preventing issues from rapid double-clicks.
- Replaces the previously planned standalone `interrupt_agents` API with a simpler flag on the existing `start` command.
