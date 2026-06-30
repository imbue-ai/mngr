Fixed messaging an agent whose process had exited but whose tmux session was still alive (e.g. after a ctrl-c, a crash, or an out-of-memory kill of just the agent process). Previously the message was typed into the leftover shell and lost; now the agent is restarted and the message delivered.

This had two parts:

- Lifecycle detection no longer mis-reports such an agent as `REPLACED`. A known-type agent whose pane has dropped back to a shell prompt is now `DONE` even when non-shell background processes are still running under the pane -- in particular mngr's own in-pane helpers (the transcript streamers and background-task script, each running a `sleep` loop), which always linger after the agent process is killed. Only a non-shell process in the pane *foreground* is treated as a genuine replacement. Unknown-agent-type behavior is unchanged.

- Sending a message now restarts an agent that is `STOPPED` or `DONE` (neither has a live process to receive the message). For a `DONE` agent the lingering tmux session is torn down first, mirroring `mngr start --restart`, so the relaunch actually happens rather than no-op'ing on the existing session.

Together this is what the OOM revival path relies on: an agent whose main process was shed is brought back by the next message it receives.
