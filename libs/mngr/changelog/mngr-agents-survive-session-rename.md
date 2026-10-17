Renaming an agent no longer breaks anything that found its tmux session by the old name.

- New agents' launch commands no longer carry the tmux session name; they read the session they run in. `mngr start` rewrites the session name out of the stored launch command of an agent created by an older mngr (the claude, codex and antigravity agent types say what to rewrite), so a renamed agent's background tasks start for the right session again. An older agent keeps the background-task scripts it was created with, so its background tasks still end on a live rename until its next restart.

- The resize hook set on each agent session finds the session by the name it has when the hook fires, so a renamed agent's window again refits to a client that attaches (the terminal app, `mngr connect`).

- On a remote host, Ctrl-q / Ctrl-t during `mngr connect` name their signal file for the agent id, so the stop or destroy still happens after the agent is renamed while attached. `mngr connect` clears any signal file an earlier connect left behind before it attaches, so a plain detach can no longer stop or destroy the agent, and it runs the stop or destroy by agent id and host (`ID@HOST`), so a same-id agent on another host is left alone.

- `imbue.mngr.utils.testing` has helpers for exercising tmux behavior and an agent plugin's background-tasks supervisor against a real tmux server: `renamed_tmux_session`, `terminal_tmux_server`, `create_background_tasks_state_dir`, `build_background_tasks_supervisor_env`, `running_background_tasks_supervisor` and `is_background_tasks_supervisor_polling`.
