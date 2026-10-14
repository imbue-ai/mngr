A renamed codex agent keeps working.

- A codex agent that was renamed and then stopped and started came back without its app-server, so its TUI could not connect and the agent sat stopped: its stored launch command spawned the app-server window into the session's old name. The launch command now finds its session when it runs, and `mngr start` upgrades the stored command of an agent created before this change.

- The transcript keeps streaming after the agent (or its minds chat) is renamed, so `mngr transcript` keeps growing. The background-tasks supervisor that runs the raw transcript streamer, the common transcript converter and the usage writer tracks the agent's tmux session by its session id, which a rename does not change. For an agent created by an older mngr, `mngr start` also rewrites the session name its stored command passes the supervisor, so its transcript runs again after its next restart.

- The supervisor's pidfile is keyed on the agent id, so a new agent that takes a renamed agent's old name gets its own supervisor.
