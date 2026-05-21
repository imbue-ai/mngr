Add `--restart` and `--no-resume` flags to `mngr start`.

- `mngr start my-agent --restart` stops a running agent and starts it fresh. If the agent is already stopped, it is simply started.
- `mngr start my-agent --no-resume` skips sending the resume message after starting. Can be combined with `--restart`.
