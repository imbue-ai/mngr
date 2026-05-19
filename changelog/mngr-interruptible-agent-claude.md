Add `--restart` flag to `mngr start` for cleanly restarting agents.

- `mngr start my-agent --restart` stops a running agent and starts it fresh without sending a resume message. If the agent is already stopped, it is simply started.
