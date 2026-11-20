On macOS, an agent keeps its background-task supervisor (which streams the transcript during a turn and records activity) after `mngr create <name> --reuse --update` on a stopped agent, and after two restarts of the same agent at once. Before, the supervisor was lost in most such runs, and each one also left a `ttyd` running.

- On macOS, `mngr stop`, `mngr start`, `mngr start --restart` and `mngr destroy` now also kill an agent's leftover helpers: your own `ttyd`, `bash` and `sh` processes reparented to pid 1 whose arguments name the agent's `commands/` directory, such as its background-task supervisor and its `ttyd`. Anything else keeps running, such as an editor opened on a file in the agent's directory or a dev server started from the agent. Linux still stops every process carrying the agent's `MNGR_AGENT_ID`.

- If that macOS process scan fails, `mngr stop` and `mngr destroy` report it as a cleanup failure.

- Existing agents need no recreate: the next stop or start run by this version of mngr cleans up. An agent created before 2026-06-24 still keeps an orphaned `ttyd` on macOS, since its stored `ttyd` command does not name the `commands/` directory.
