Fix `mngr conn` to auto-restart a local agent whose tmux session has died out of band.
Previously, if the agent's stored lifecycle state still said WAITING/RUNNING but the tmux
session had been killed (e.g. by `tmux kill-session`, an OS reboot, or a sleep/wake glitch),
`mngr conn` would leak tmux's bare `can't find session` error and not restart the agent.
The connect path now probes the local tmux session via `tmux has-session` before attaching
and, when missing, either starts the agent (default) or raises a clear `UserInputError`
with a suggested `mngr start <agent>` recovery (under `--no-start`).
