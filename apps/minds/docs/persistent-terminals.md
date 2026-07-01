# Terminal persistence in minds workspaces

The terminals in the minds dockview are backed by named [tmux](https://github.com/tmux/tmux/wiki) sessions. Each "New terminal" tab attaches to (or creates) its own session, so the terminal's shell, working directory, running processes, and in-memory scrollback survive:

- closing the tab and reopening it,
- reloading the workspace,
- and the terminal service itself restarting.

They do **not** survive a container/host restart: the tmux server lives in memory and is cleared when the container stops, so a restored tab comes back as a fresh shell (with a one-line notice) rather than its previous state.

This is intentional. Nothing about your terminals -- input, output, commands, or scrollback -- is written to disk or included in any backup, which keeps secrets that pass through a terminal from being persisted anywhere.

## Making terminal state persistent (opt-in)

> This section is a placeholder. A full tutorial is planned.

If you understand the security trade-offs and want your terminal history, scrollback, or shell state to survive a container restart, you can opt in yourself with standard bash and tmux configuration (for example, a persistent `HISTFILE` on the backed-up volume, `shopt -s histappend`, and tmux logging via `pipe-pane`). Because this writes terminal contents to disk, only enable it for workspaces where that is acceptable.
